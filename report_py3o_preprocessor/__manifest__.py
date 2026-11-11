# Copyright 2026 Open Eye Development
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
{
    "name": "Py3o Report Engine — Jinja2-style Template Preprocessor",
    "summary": "Author py3o templates with {{ var }} / {% for %} tokens in LibreOffice. "
    "Source templates are auto-compiled to the py3o hyperlink/user-field XML that "
    "report_py3o renders.",
    "version": "19.0.1.0.0",
    "category": "Reporting",
    "license": "AGPL-3",
    "author": "Open Eye Development, Odoo Community Association (OCA)",
    "website": "https://github.com/OCA/reporting-engine",
    "depends": ["report_py3o"],
    "external_dependencies": {"python": ["lxml"]},
    "data": [
        "security/ir.model.access.csv",
        "views/py3o_template_views.xml",
    ],
    "installable": True,
}
