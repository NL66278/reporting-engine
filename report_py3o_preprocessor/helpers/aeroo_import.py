# Copyright 2026 Open Eye Development
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Aeroo → report_py3o_preprocessor source converter.

Aeroo report templates (OCA ``report_aeroo``) store dynamic expressions inside
``<text:placeholder text:placeholder-type="text">…</text:placeholder>`` elements,
using Genshi-style angle-bracket syntax::

    <text:placeholder>&lt;objects[0].name&gt;</text:placeholder>
    <text:placeholder>&lt;for each=&quot;x in xs&quot;&gt;</text:placeholder>
    <text:placeholder>&lt;/for&gt;</text:placeholder>

This module rewrites such a template into the *source* format that
:mod:`helpers.preprocessor` consumes — plain-text Jinja2-style tokens
(``{{ expr }}`` / ``{% for %}`` / ``{% if %}`` / ``{# comment #}``) sitting in
the paragraph body, with no ``<text:placeholder>`` wrapper. The output is a
``.src.odt`` you can upload into a ``py3o.template`` record (or reference via
``py3o_template_fallback``) and let the preprocessor compile.

Translation table
================

============================  ============================================
Aeroo (inside ``< … >``)     py3o preprocessor source
============================  ============================================
``objects[0].field``          ``{{ objects[0].field }}``
``o.field``                   ``{{ objects[0].field }}``  (single-record alias)
``for each="x in xs"``        ``{% for x in xs %}``
``/for``                      ``{% endfor %}``
``if test="cond"``            ``{% if cond %}``
``/if``                      ``{% endif %}``
``setLang('xx_XX')``          ``{# setLang('xx_XX') dropped: py3o has no
                              setLang; use o_format_lang(value, lang_code=…) #}``
``formatLang(expr)``         ``{{ o_format_lang(expr) }}``
``<any other expr>``          ``{{ <any other expr> }}``
``<<expr>>`` (typo)           ``{{ expr }}``  (lint flags the strip)
============================  ============================================

What this converter does NOT do
===============================

* It does **not** evaluate or validate the Python expressions — that's the
  preprocessor's job (run :func:`helpers.preprocessor.lint` on the output).
* It does **not** handle Aeroo ``<choose>``/``<when>``/``<otherwise>`` blocks
  (no py3o equivalent). If encountered, the converter emits a warning and
  leaves the placeholder untouched so the author can refactor manually.
* It does **not** touch ``<text:user-field-get>``, ``<text:expression>`` or
  other OpenDocument field elements — only Aeroo's ``<text:placeholder>``.

Output format
=============

Each ``<text:placeholder>`` element is replaced in place by a style-less
``<text:span>`` carrying the Jinja2-style token as plain text. The span's
``.tail`` preserves whatever text followed the placeholder. The resulting
``.odt`` opens cleanly in LibreOffice (tokens show as plain editable text)
and is parsed correctly by :mod:`helpers.preprocessor` (tokens are picked up
via the paragraph's flattened ``itertext()``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path

from lxml import etree

from .odt_io import load, dump
from .preprocessor import Issue

# Namespace URIs (Clark notation).
_NS = {
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}


def _qn(prefix: str, tag: str) -> str:
    return f"{{{_NS[prefix]}}}{tag}"


# ---------------------------------------------------------------------------
# Aeroo expression classification
# ---------------------------------------------------------------------------

# ``<for each="x in xs">`` → capture target + iterable.
_FOR_EACH_RE = re.compile(r'^for\s+each\s*=\s*"([^"]+)"\s*$')
# ``<if test="cond">`` → capture condition.
_IF_TEST_RE = re.compile(r'^if\s+test\s*=\s*"([^"]+)"\s*$')
# ``<setLang('xx_XX')>`` — Aeroo language directive; arg can be quoted either way.
_SETLANG_RE = re.compile(r"^setLang\s*\((.*)\)\s*$")
# ``<formatLang(...)>`` — Aeroo formatting helper; renamed to py3o's o_format_lang.
_FORMATLANG_RE = re.compile(r"^formatLang\s*\((.*)\)\s*$", re.DOTALL)
# ``o.<field>`` — Aeroo single-record alias. Rewrite to ``objects[0].<field>``.
# Negative lookbehind on identifier chars AND ``.`` so we only match ``o`` at
# the start of a dotted access chain (``o.x``, ``(o.x)``, ``, o.x``) and not
# a field named ``o`` accessed via something else (``foo.o.x``, ``objects[0].o.x``).
_O_ALIAS_RE = re.compile(r"(?<![A-Za-z0-9_.])o\.")
# Aeroo when test="…" — no py3o equivalent (choose/when/otherwise).
_WHEN_RE = re.compile(r'^when\s+test\s*=\s*"([^"]+)"\s*$')


@dataclass
class _ConvertState:
    """Accumulator for issues during a conversion run."""

    issues: list[Issue] = field(default_factory=list)
    n_converted: int = 0
    n_skipped: int = 0


def _classify(raw: str, state: _ConvertState, location: str) -> str | None:
    """Translate one Aeroo expression (with outer ``<…>`` already stripped)
    into a Jinja2-style source token.

    Returns the token string, or ``None`` if the expression is not
    translatable (in which case an issue is appended so the caller leaves
    the placeholder untouched).
    """
    expr = raw.strip()
    if not expr:
        state.issues.append(Issue("error", "Empty Aeroo placeholder", location))
        return None

    # --- Closings ---
    if expr == "/for":
        return "{% endfor %}"
    if expr == "/if":
        return "{% endif %}"
    if expr in ("/choose", "/when", "/otherwise"):
        state.issues.append(
            Issue(
                "warning",
                f"Aeroo closing {expr!r} has no py3o equivalent (choose/when/"
                "otherwise not supported). Refactor manually.",
                location,
            )
        )
        return None

    # --- for each="…" ---
    m = _FOR_EACH_RE.match(expr)
    if m:
        # Aeroo puts the full ``x in xs`` inside the quotes.
        body = m.group(1).strip()
        # Validate shape ``<target> in <iter>``; defer detailed grammar check
        # to the preprocessor (which will run on the converted source).
        if " in " not in body:
            state.issues.append(
                Issue(
                    "error",
                    f"Aeroo for-each body {body!r} missing 'in' keyword.",
                    location,
                )
            )
            return None
        # Apply the ``o.`` → ``objects[0].`` rewrite to the iterable part
        # (after `` in ``), not the loop target. The target is a fresh binding.
        target, _, iter_expr = body.partition(" in ")
        rewritten_iter = _O_ALIAS_RE.sub("objects[0].", iter_expr)
        if rewritten_iter != iter_expr:
            state.issues.append(
                Issue(
                    "info",
                    f"Rewrote Aeroo single-record alias 'o.' → 'objects[0].' "
                    f"in for-each iterable {iter_expr!r}.",
                    location,
                )
            )
        return "{% for " + target.strip() + " in " + rewritten_iter.strip() + " %}"

    # --- if test="…" ---
    m = _IF_TEST_RE.match(expr)
    if m:
        cond = m.group(1).strip()
        rewritten_cond = _O_ALIAS_RE.sub("objects[0].", cond)
        if rewritten_cond != cond:
            state.issues.append(
                Issue(
                    "info",
                    f"Rewrote Aeroo single-record alias 'o.' → 'objects[0].' "
                    f"in if-test condition {cond!r}.",
                    location,
                )
            )
        return "{% if " + rewritten_cond + " %}"

    # --- choose / when / otherwise (no py3o equivalent) ---
    if expr == "choose" or _WHEN_RE.match(expr) or expr == "otherwise":
        state.issues.append(
            Issue(
                "warning",
                f"Aeroo {expr!r} has no py3o equivalent (choose/when/otherwise "
                "not supported). Refactor as nested {{% if %}} blocks manually.",
                location,
            )
        )
        return None

    # --- setLang('xx') → drop + comment ---
    m = _SETLANG_RE.match(expr)
    if m:
        arg = m.group(1).strip()
        return (
            "{# setLang(" + arg + ") dropped on Aeroo→py3o conversion: "
            "py3o has no setLang; use o_format_lang(value, lang_code=...) "
            "or set the report action's lang. #}"
        )

    # --- formatLang(…) → o_format_lang(…) ---
    m = _FORMATLANG_RE.match(expr)
    if m:
        return "{{ o_format_lang(" + m.group(1) + ") }}"

    # --- plain expression (variable substitution) ---
    # Apply the ``o.`` → ``objects[0].`` rewrite to the expression body.
    rewritten = _O_ALIAS_RE.sub("objects[0].", expr)
    if rewritten != expr:
        state.issues.append(
            Issue(
                "info",
                f"Rewrote Aeroo single-record alias 'o.' → 'objects[0].' "
                f"in {expr!r}.",
                location,
            )
        )
    return "{{ " + rewritten + " }}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def convert_aeroo(data: bytes, src_format: str = "odt") -> tuple[bytes, list[Issue]]:
    """Convert an Aeroo ``.odt``/``.fodt`` template to py3o-preprocessor source.

    :param data:       raw bytes of the Aeroo template.
    :param src_format: ``"odt"`` (zipped, default) or ``"fodt"`` (flat XML).
    :returns:          ``(converted_bytes, issues)``. The converted bytes are
                       in the same format as the input. ``issues`` is the list
                       of :class:`helpers.preprocessor.Issue` records produced
                       during conversion (errors block a clean run; warnings
                       flag constructs the converter refused to translate;
                       info entries note automatic rewrites).
    """
    content_xml, mimetype, extras = load(data, src_format)
    state = _ConvertState()
    try:
        new_content = _transform(content_xml, state)
        # ODF keeps page headers/footers in styles.xml, NOT in content.xml —
        # Aeroo placeholders can legitimately live there (the foei template
        # has one for ``objects[0].project_id.reference`` in the page header).
        # Process styles.xml the same way; py3o's
        # ``get_all_user_python_expression`` scans all templated_files
        # (content.xml + styles.xml + manifest.xml), so emits placed here are
        # picked up at render time without further work.
        new_extras: list[tuple[str, bytes]] = []
        for name, raw in extras:
            if name == "styles.xml":
                raw = _transform(raw, state)
            new_extras.append((name, raw))
        extras = new_extras
    except etree.XMLSyntaxError as exc:
        state.issues.insert(
            0, Issue("error", f"Aeroo source contains invalid XML: {exc}", "")
        )
        return data, state.issues
    out = dump(new_content, src_format, mimetype, extras)
    return out, state.issues


def convert_aeroo_file(
    source_path: str, target_path: str | None = None
) -> tuple[str, list[Issue]]:
    """Convert an Aeroo template file on disk.

    :returns: ``(written_path, issues)``. If ``target_path`` is None, the
              output is written next to the source with ``.src`` inserted
              before the extension (``foo.odt`` → ``foo.src.odt``), matching
              the preprocessor's filesystem convention.
    """
    p = Path(source_path)
    raw = p.read_bytes()
    src_format = "fodt" if p.suffix == ".fodt" else "odt"
    out, issues = convert_aeroo(raw, src_format)
    if target_path is None:
        # ``foo.odt`` → ``foo.src.odt``; ``foo.fodt`` → ``foo.src.fodt``.
        target = p.with_suffix(".src" + p.suffix)
    else:
        target = Path(target_path)
    target.write_bytes(out)
    return str(target), issues


# ---------------------------------------------------------------------------
# Transformation
# ---------------------------------------------------------------------------


def _transform(content_xml: bytes, state: _ConvertState) -> bytes:
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.fromstring(content_xml, parser=parser)
    placeholders = list(tree.iter(_qn("text", "placeholder")))
    for ph in placeholders:
        _replace_placeholder(ph, state)
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8")


def _replace_placeholder(ph: etree._Element, state: _ConvertState) -> None:
    """Replace one ``<text:placeholder>`` with a span carrying the token."""
    location = _location(ph)
    # The placeholder's text is the XML-escaped Aeroo expression, e.g.
    # ``&lt;objects[0].name&gt;``. lxml unescapes on .text access, so
    # ``ph.text`` already returns the unescaped ``<objects[0].name>``.
    raw_text = ph.text or ""
    if not raw_text:
        state.issues.append(
            Issue("warning", "Empty <text:placeholder> element", location)
        )
        state.n_skipped += 1
        return
    aeroo_expr = unescape(raw_text).strip()

    # Strip outer ``<…>`` layers. Aeroo wraps every expression in one pair;
    # a stray ``<<…>>`` (authoring typo) wraps it in two. Count layers so we
    # can flag the typo without mis-handling the inner expression.
    layers = 0
    inner = aeroo_expr
    while inner.startswith("<") and inner.endswith(">") and len(inner) >= 2:
        inner = inner[1:-1].strip()
        layers += 1
        if layers > 4:  # sanity guard against pathological input
            break
    if layers == 0:
        state.issues.append(
            Issue(
                "info",
                f"Placeholder text {aeroo_expr!r} is not wrapped in <…>; "
                f"treating as a bare expression.",
                location,
            )
        )
        inner = aeroo_expr
    elif layers >= 2:
        state.issues.append(
            Issue(
                "info",
                f"Stray double-bracket ({layers} layers of <…>) in "
                f"{aeroo_expr!r}; extra brackets stripped, inner used as "
                f"expression.",
                location,
            )
        )

    token = _classify(inner, state, location)
    if token is None:
        state.n_skipped += 1
        return

    # Build a style-less span carrying the Jinja token as plain text.
    # Preserve the placeholder's tail (text after it in the parent).
    parent = ph.getparent()
    if parent is None:
        state.issues.append(
            Issue("warning", "Placeholder has no parent; skipped.", location)
        )
        state.n_skipped += 1
        return
    span = etree.Element(_qn("text", "span"))
    span.text = token
    span.tail = ph.tail
    # Replace in place (preserves position among siblings).
    idx = list(parent).index(ph)
    parent.remove(ph)
    parent.insert(idx, span)
    state.n_converted += 1


def _location(el: etree._Element) -> str:
    parent = el.getparent()
    if parent is None:
        return "root"
    try:
        idx = list(parent).index(el) + 1
    except ValueError:
        idx = 0
    return f"{parent.tag}/{el.tag}[{idx}]"
