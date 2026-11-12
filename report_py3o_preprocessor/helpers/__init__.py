# Copyright 2026 Open Eye Development
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Pure-Python preprocessor for Jinja2-style py3o template authoring.

This subpackage has no Odoo dependency. The transformer in
:mod:`helpers.preprocessor` takes a LibreOffice ``.odt``/``.fodt`` source
template (containing ``{{ expr }}`` / ``{% for %}`` / ``{% if %}`` /
``{% function= %}`` / ``{# comment #}`` tokens typed as ordinary text) and
produces a compiled template with the py3o hyperlink + user-field XML that
``report_py3o`` (OCA/reporting-engine) renders.

The library is intentionally standalone so it can be unit-tested offline and
later extracted to a dedicated pip package if useful.
"""
from .aeroo_import import convert_aeroo, convert_aeroo_file
from .preprocessor import (
    Issue,
    PreprocessError,
    lint,
    preprocess,
    preprocess_file,
)

__all__ = [
    "Issue",
    "PreprocessError",
    "convert_aeroo",
    "convert_aeroo_file",
    "lint",
    "preprocess",
    "preprocess_file",
]
