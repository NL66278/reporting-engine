==============
report_py3o_preprocessor
==============

Author py3o report templates using ordinary Jinja2-style tokens in LibreOffice.
This module builds on the templating model that ``py3o.template`` (and OCA
``report_py3o``) established — hyperlink-based control flow and named user
fields for variables — and adds a thin authoring layer so templates can be
written with the more familiar ``{{ }}`` / ``{% %}`` syntax.

Why
===

``py3o.template``'s templating model maps OpenDocument structures directly to
Genshi control flow: a *for* loop is a hyperlink whose URL is
``py3o://for="x in xs"`` and whose visible text matches that payload; a
variable is a named user field with the ``py3o.`` prefix. This design has real
strengths — it stays inside the document's XML, so the template remains a
valid, editable ODF file that opens cleanly in LibreOffice, and the same
mechanism also carries images, frames, and format functions.

The trade-off is that authoring those hyperlinks and user fields by hand is
tedious and error-prone: the visible text must match the URL payload exactly,
closings use a separate three-slash ``py3o:///for`` hyperlink, every printed
variable needs a matching declaration block, and the validation py3o does
happens at render time (so a typo usually shows up as a raw Python traceback
through LibreOffice).

This module lets you author the same templates with Jinja2-style syntax that
any web developer already recognises:

::

    {% for item in items %}
    Order {{ item.name }} — {{ o_format_lang(item.amount_total) }}
    {% endfor %}

    {% if user.amount > 0 %}
    Balance due.
    {% endif %}

The tokens are automatically compiled into the py3o hyperlink + user-field
XML that ``report_py3o`` renders. Both the source (your ``{{ }}`` version) and
the compiled template stay attached to the same ``py3o.template`` record.

Syntax
======

The preprocessor accepts Jinja2-style split markers:

``{{ <expression> }}``
    Variable substitution. Emits a py3o user field whose value is genshi's
    evaluation of ``<expression>``. Examples: ``{{ user.name }}``,
    ``{{ o_format_lang(amount) }}``, ``{{ line.partner_id.name }}``.

``{% for <name> in <expr> %}`` … ``{% endfor %}``
    Iterates a recordset or sequence. Each loop iteration binds ``<name>``.

``{% if <expr> %}`` … ``{% endif %}``
    Conditional block (genshi/Python expression).

``{% function="<name>(<args>)" %}``
    A py3o format-function hyperlink (e.g. ``{% function="format_currency(amount, 'EUR')" %}``).

``{# <comment> #}``
    Comment — stripped from output. Useful for in-document author notes that
    shouldn't appear in the rendered report.

Not supported (will raise an error at compile time)
====================================================

``{% elif %}`` and ``{% else %}`` are not part of ``py3o.template``'s
control-flow vocabulary (it provides ``for``/``if`` and their closings, plus
``function``). Passing them through unchanged would surface as a genshi/Python
error at render time, so this module refuses them at compile time with a
pointer to the workaround. Refactor as two adjacent ``{% if %}`` blocks::

    {% if condition %}
    …
    {% endif %}
    {% if not condition %}
    …
    {% endif %}

(The condition expression is whatever genshi supports — ``not``,
``and``/``or``, dotted attribute access, comparisons.)

Block rule (Liquid-style)
=========================

If a paragraph's *only* non-whitespace content is a single ``{% … %}`` directive,
that paragraph becomes the directive's container (the visible text is replaced
with the py3o link element). This lets you write::

    <text:p>{% for line in lines %}</text:p>

and it correctly produces a standalone py3o "for" link paragraph — no special
LibreOffice formatters required. ``{{ … }}`` and ``{# … #}`` are always inline.

How to use it
=============

Database-stored templates (the easy path)
-----------------------------------------

1. Reporting → Configuration → Py3o → Py3o Templates. Open (or create) a
   template record.
2. Pick a **File Type** (``odt`` for a normal LibreOffice Writer file).
3. Upload your ``.odt`` (which contains ``{{ … }}`` / ``{% … %}`` tokens) into
   the new **Source Template (Jinja2-style)** field. The download filename
   you set becomes its name suffix.
4. As soon as you save, the preprocessor runs automatically and populates the
   stock **LibreOffice Template** field with the compiled py3o XML. A status
   badge shows ``Compiled`` (success), ``Error`` (click **Diagnostics** for
   the lint output), or ``Empty``.
5. Re-uploading the source recompiles automatically.

Filesystem templates (developer path)
-------------------------------------

Author your template at ``<my_module>/report/account_invoice.src.odt`` —
note the ``.src.`` suffix that opts the file into preprocessing. Reference it
from your report action via ``py3o_template_fallback = "report/account_invoice.src.odt"``
(or an absolute trusted path — see ``report_py3o`` docs for ``root_tmpl_path``).

At render time, the preprocessor runs against the source, with a
content-hash cache (``ir.attachment`` linked to the report action) so the
preprocessor only re-runs when the source changes.

Available parser-context helpers
================================

Whatever ``report_py3o`` already exposes is available inside your ``{{ }}``
expressions:

* ``user``, ``lang``, ``time``
* ``o_format_lang(value, digits=None, monetary=False, dp=False, currency_obj=False, …)``
* ``o_format_date(value, lang_code=False, date_format=False)``
* ``display_address(partner)``
* ``format_multiline_value(string)`` — emits ODF ``<text:line-break/>`` for ``\n``
* ``html_sanitize(string)``, ``b64decode(bytes)``
* ``objects`` — the recordset the report was launched on (``object`` for single-record reports; check the report action's binding model)

Format functions from py3o.template itself:

* ``format_currency(number, currency_code=None, format=None, …)``
* ``format_datetime(date_obj, format=None, locale=None)``

(wrap these inside ``{% function="…" %}``.)

Reference: see the upstream py3o.template templating docs at
https://py3otemplate.readthedocs.io/en/latest/templating.html

Limitations in this version (19.0.1.0.0)
========================================

* **Flat-XML ``.fodt`` sources on disk** are loaded but the resulting compiled
  output is also ``.fodt``; ``py3o.template.Template`` only loads zipped
  ``.odt`` from ``py3o_template_fallback``, so prefer saving your
  LibreOffice template as ``.odt``.
* Tables: ``{% for %}`` directives that wrap a table row need the
  ``{% for %}`` and ``{% endfor %}`` tags to sit in their own paragraph inside
  the first/last cell of the row — the same row-loop pattern ``report_py3o``
  uses today. This module honours the surrounding structure py3o expects; see
  the upstream docs.

Tests
=====

The pure-Python preprocessor library lives under ``helpers/`` and has no
Odoo dependency. Unit tests run against Python 3.13 + lxml + ``py3o.template``::

    cd report_py3o_preprocessor
    python tests/test_preprocessor.py

Expect ``ALL TESTS PASSED``. Tests cover tokenizer behaviour, the block rule,
each token kind (``{{ }}``/``{% for %}``/``{% if %}``/``{% function %}``/``{# #}``),
unbalanced-directive detection, the ``{% elif %}`` refusal, and an
end-to-end acceptance test that feeds the compiled ``.odt`` to
``py3o.template.Template.get_all_user_python_expression()``.

Licensing
=========

AGPL-3 to match ``report_py3o`` upstream. Designed to be upstreamable to
OCA/reporting-engine as ``report_py3o_preprocessor`` (or a more
value-descriptive name) once stabilised.
