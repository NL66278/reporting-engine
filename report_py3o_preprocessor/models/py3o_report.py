# Copyright 2026 Open Eye Development
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Filesystem-source support for py3o reports.

Authors may keep their Jinja2-style source template on disk under the
convention ``<basename>.src.<ext>`` (e.g. ``report/account_invoice.src.odt``)
inside their Odoo module directory and reference it via
:field:`ir.actions.report.py3o_template_fallback`. At render time, the source
is precompiled into py3o hyperlink/user-field XML and the result is returned
to ``report_py3o`` for normal rendering.

A content-hash cache (``ir.attachment`` linked to the report action) avoids
re-running the preprocessor on every render; the cache is invalidated
automatically when the source file's SHA-256 changes.
"""
from __future__ import annotations

import hashlib
import logging
import os

from odoo import _, api, models
from odoo.tools import misc

from ..helpers.preprocessor import preprocess

_logger = logging.getLogger(__name__)

# AttachmentCounts constants. Keying cache files on the report action id and
# sha of the source file content narrows the scope so multiple histories of
# the same file (across module upgrades) don't collide.
_ATTACH_KEY_PREFIX = "report_py3o_preprocessor_cache"


class Py3oReport(models.TransientModel):
    _inherit = "py3o.report"

    @api.model
    def _is_src_template_path(self, tmpl_name: str) -> bool:
        """:returns: ``True`` iff ``tmpl_name`` follows the ``.src.<ext>``
        convention used to opt a filesystem template into preprocessing."""
        if not tmpl_name:
            return False
        # Allow both "report/x.src.odt" and "x.src.fodt"; the convention is
        # purely suffix-based and a plain ``.odt`` source is *not* compiled
        # (falls through to report_py3o's stock loader).
        base = os.path.basename(tmpl_name)
        return ".src." in base

    def _get_template_from_path(self, tmpl_name):
        """Override to transparently preprocess ``.src.<ext>`` sources."""
        if not self._is_src_template_path(tmpl_name):
            return super()._get_template_from_path(tmpl_name)

        # Locate the actual file on disk. We rely on report_py3o's own logic
        # (module-relative via pkg_resources or trusted absolute path) —
        # duplicate its lookup so we don't tightly couple to private methods.
        report_xml = self.ir_actions_report_id
        src_filename = None
        if report_xml.module:
            try:
                import importlib

                module_path = importlib.import_module(
                    f"odoo.addons.{report_xml.module}"
                ).__path__[0]
                src_filename = os.path.realpath(os.path.join(module_path, tmpl_name))
                # Reject if it escaped the module directory.
                if not src_filename.startswith(
                    os.path.realpath(module_path) + os.path.sep
                ):
                    _logger.warning(
                        "Py3o source path %s escapes module %s; ignored.",
                        tmpl_name,
                        report_xml.module,
                    )
                    return None
            except (ImportError, IndexError):
                _logger.warning(
                    "Cannot resolve module %r for source template %s",
                    report_xml.module,
                    tmpl_name,
                )
                return None
        elif self._is_valid_template_path(tmpl_name):
            src_filename = os.path.realpath(tmpl_name)
        else:
            return None

        if not src_filename or not os.path.isfile(src_filename):
            _logger.warning("Py3o source template not found at %s", src_filename)
            return None

        with open(src_filename, "rb") as fh:
            src_bytes = fh.read()

        src_format = src_filename.rsplit(".", 1)[-1].lower()
        if src_format not in ("odt", "ods", "odp", "fodt", "fods", "fodp"):
            _logger.warning(
                "Unsupported py3o source extension %s for %s",
                src_format,
                src_filename,
            )
            return None
        try:
            # The preprocessor preserves the source format: ``.fodt`` source
            # produces ``.fodt`` output and ``.odt`` source produces ``.odt``
            # output. report_py3o hands the bytes to ``py3o.template.Template``
            # which only loads ``.odt`` (zipped) — so ``.fodt`` sources aren't
            # viable for the inline render path; instruct the author to save
            # as ``.odt`` instead.
            if src_format.startswith("fod"):
                _logger.warning(
                    "Py3o source template %s is flat XML (.fodt), but "
                    "py3o.template only loads zipped .odt. Save as .odt and "
                    "rename to *.src.odt to fix.",
                    src_filename,
                )
                return None
            compiled = self._cached_preprocess(
                report_xml, src_filename, src_bytes, src_format
            )
            return compiled
        except Exception as exc:  # pragma: no cover
            _logger.exception("Preprocessing failed for %s: %s", src_filename, exc)
            return None

    # ------------------------------------------------------------------
    # Compile cache
    # ------------------------------------------------------------------

    def _cached_preprocess(
        self,
        report_xml,
        src_filename: str,
        src_bytes: bytes,
        src_format: str,
    ) -> bytes:
        """Look up the ir.attachment cache for ``src_filename``; build if miss.

        Cache key: a hash of (<report_xml.id>, <src_filename>, <sha256 of
        bytes>). On hit, the cached compiled ``.odt`` bytes are returned
        directly. On miss, the preprocessor runs and the result is stored
        for the next render.
        """
        sha = hashlib.sha256(src_bytes).hexdigest()
        attach_model = self.env["ir.attachment"].sudo()
        # Self-managed records under res_model='ir.actions.report' so the
        # cache is purged when the report action is deleted.
        cached = attach_model.search(
            [
                ("res_model", "=", "ir.actions.report"),
                ("res_id", "=", report_xml.id),
                ("name", "=", self._cache_name(src_filename, sha)),
            ],
            limit=1,
        )
        if cached:
            return cached.datas and __import__("base64").b64decode(cached.datas) or b""

        # Miss — preprocess and persist.
        compiled = preprocess(src_bytes, src_format=src_format)
        # Prune stale cache entries for the same source filename (any hash).
        attach_model.search(
            [
                ("res_model", "=", "ir.actions.report"),
                ("res_id", "=", report_xml.id),
                ("name", "like", self._cache_name_prefix(src_filename)),
            ]
        ).unlink()
        import base64 as _b64

        attach_model.create(
            {
                "name": self._cache_name(src_filename, sha),
                "res_model": "ir.actions.report",
                "res_id": report_xml.id,
                "datas": _b64.b64encode(compiled).decode("ascii"),
                "mimetype": "application/vnd.oasis.opendocument.text",
            }
        )
        return compiled

    @api.model
    def _cache_name(self, src_filename: str, sha: str) -> str:
        base = os.path.basename(src_filename)
        # File-name shape: "report_py3o_preprocessor_cache:<basename>:<sha16>"
        return f"{_ATTACH_KEY_PREFIX}:{base}:{sha[:16]}"

    @api.model
    def _cache_name_prefix(self, src_filename: str) -> str:
        base = os.path.basename(src_filename)
        return f"{_ATTACH_KEY_PREFIX}:{base}:%"
