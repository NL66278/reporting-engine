# Copyright 2026 Open Eye Development
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Extend ``py3o.template`` with a source-template field.

Authors upload their LibreOffice file containing Jinja2-style
``{{ ... }}``/``{% ... %}`` tokens into :field:`source_template_data`. The
:mod:`helpers.preprocessor` library compiles it automatically into the py3o
hyperlink/user-field XML that report_py3o renders, and stores the result in
the stock :field:`py3o_template_data` field. Both source and compiled versions
live on the same record.

Re-uploading the source template triggers an automatic recompile. Direct
uploads to ``py3o_template_data`` (the legacy path — authoring the raw py3o
XML by hand) keep working unchanged.
"""
from __future__ import annotations

import logging
from base64 import b64decode, b64encode

from odoo import _, api, fields, models

from ..helpers.preprocessor import PreprocessError, lint, preprocess

_logger = logging.getLogger(__name__)


class Py3oTemplate(models.Model):
    _inherit = "py3o.template"

    source_template_data = fields.Binary(
        string="Source Template (Jinja2)",
        help=(
            "Upload your LibreOffice template containing {{ var }} and "
            "{% for x in y %}{% endfor %} tokens here. The compiled "
            "py3o template is auto-generated whenever this field changes."
        ),
    )
    source_template_filename = fields.Char(
        string="Source File Name",
        help="Filename of the uploaded source template (kept for display + "
        "extension inference).",
    )
    preprocessor_status = fields.Selection(
        selection=[
            ("ok", "Compiled"),
            ("error", "Error"),
            ("empty", "Empty (no source attached)"),
        ],
        string="Preprocessor Status",
        readonly=True,
        default="empty",
    )
    preprocessor_issues = fields.Text(
        string="Preprocessor Diagnostics",
        readonly=True,
        help="Lint output produced by the last preprocessing pass. Empty "
        "when the source compiled cleanly.",
    )

    # ------------------------------------------------------------------
    # ORM overrides — auto-compile source -> compiled
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._compile_source_templates()
        return records

    def write(self, vals):
        # Determine whether we'll need to recompile after this write.
        should_recompile = (
            not self.env.context.get("_report_py3o_preprocessor_compiling")
            and ("source_template_data" in vals or "filetype" in vals)
        )
        res = super().write(vals)
        if should_recompile:
            self._compile_source_templates()
        return res

    # ------------------------------------------------------------------
    # Compile helpers
    # ------------------------------------------------------------------

    def _compile_source_templates(self) -> None:
        """Preprocess every record that has a source template attached.

        Records whose source was cleared (set to falsy) are marked ``empty``;
        their previously-compiled ``py3o_template_data`` is left untouched so
        we don't wipe a working runtime artifact unless the author explicitly
        clears the compiled field too.
        """
        for rec in self:
            if not rec.source_template_data:
                rec._set_status("empty", "")
                continue
            src_bytes = b64decode(rec.source_template_data)
            src_format = (rec.filetype or "odt").lower()
            try:
                compiled = preprocess(src_bytes, src_format=src_format)
            except PreprocessError as exc:
                # Refuse to overwrite the last-good compiled template — both
                # source and (last good) compiled stay attached for diagnosis.
                rec._set_status(
                    "error",
                    "\n".join(
                        f"[{issue.severity}] {issue.location}: {issue.message}"
                        for issue in exc.issues
                    ),
                )
                _logger.warning(
                    "Preprocessor failed on py3o.template %s: %s",
                    rec.display_name,
                    exc.message,
                )
                continue
            diagnostics = lint(src_bytes, src_format=src_format)
            issues_text = (
                "\n".join(
                    f"[{i.severity}] {i.location}: {i.message}" for i in diagnostics
                )
                if diagnostics
                else ""
            )
            rec.with_context(_report_py3o_preprocessor_compiling=True).write(
                {
                    "py3o_template_data": b64encode(compiled).decode("ascii"),
                    "preprocessor_status": "ok",
                    "preprocessor_issues": issues_text,
                }
            )

    def _set_status(self, status: str, issues: str) -> None:
        """Update preprocessor status fields without triggering recompile."""
        self.with_context(_report_py3o_preprocessor_compiling=True).write(
            {
                "preprocessor_status": status,
                "preprocessor_issues": issues,
            }
        )

    def action_recompile(self) -> None:
        """Manual re-trigger in case the user wants to force compile."""
        self._compile_source_templates()

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def action_show_diagnostics(self) -> dict:
        """Open a small wizard showing lint output (basic info-only view)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Preprocessor Diagnostics"),
            "res_model": "py3o.template",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "context": {"form_view_initial_mode": "view"},
        }
