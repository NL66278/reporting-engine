# Copyright 2026 Open Eye Development
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Core preprocessor: transform Jinja2-style ``{{ }}``/``{% %}`` tokens into
the py3o hyperlink + user-field XML that ``report_py3o`` renders.

The transformer operates on the LibreOffice ``content.xml`` body (writer or
calc). It is pure-Python and has no Odoo dependency. See :mod:`helpers.odt_io`
for the readFile/writeFile wrapper around the ``.odt`` zip.

Output contract (what ``py3o.template`` expects):

* Control directives — ``{% for x in y %}``, ``{% if c %}``, ``{% else %}``,
  ``{% elif c %}``, ``{% endif %}``, ``{% endfor %}``, ``{% function="..." %}``
  — become ``<text:a xlink:href="py3o://<payload>">`` elements whose visible
  text equals ``<payload>``. Closing markers use ``py3o:///<keyword>`` with
  visible text ``/<keyword>``.

* Variable substitutions — ``{{ <expr> }}`` — become
  ``<text:user-field-get text:name="py3o.<expr>"/>`` references to a declared
  ``<text:user-field-decl text:name="py3o.<expr>"
  office:value-type="string" office:string-value="<hint>"/>``.

* Comments — ``{# ... #}`` — are stripped.

Block-rule (Liquid-style): a paragraph (<text:p> or <text:h>) whose only
non-whitespace content is a single ``{% ... %}`` directive becomes a *block*
directive — the paragraph is hollowed out and replaced by the corresponding
``<text:a>`` element as its sole child. ``{{ ... }}`` and ``{# ... #}`` are
always inline.
"""
from __future__ import annotations

import ast
import io
import urllib.parse
from dataclasses import dataclass, field
from typing import IO, Iterable

from lxml import etree

from .tokens import (
    Token,
    directive_parts,
    paragraph_is_sole_directive,
    tokenize,
)

# Namespace URIs (Clark notation is used by lxml).
NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
}


def _qn(prefix: str, tag: str) -> str:
    """Return a Clark-notation qualified name."""
    return f"{{{NS[prefix]}}}{tag}"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    """A diagnostic returned by :func:`lint`."""

    severity: str  # "error" | "warning" | "info"
    message: str
    # Best-effort context: a paragraph index or an XPath-ish location string.
    location: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "message": self.message, "location": self.location}


@dataclass
class _State:
    """Accumulator passed through the tree walk."""

    issues: list[Issue] = field(default_factory=list)
    # user-field names (with the py3o. prefix already applied) that must be
    # declared in <text:user-field-decls> for the rendered template.
    user_fields: dict[str, str] = field(default_factory=dict)  # name -> placeholder hint


class PreprocessError(Exception):
    """Raised by :func:`preprocess` when the source template is unusable.

    Carries structured issues when produced by the linter; ``str(err)`` is
    human-readable.
    """

    def __init__(self, message: str, issues: list[Issue] | None = None):
        super().__init__(message)
        self.message = message
        self.issues = issues or []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def preprocess(data: bytes, src_format: str = "odt") -> bytes:
    """Compile a source template into a py3o-ready template.

    :param data:    raw bytes of the source template (``.odt`` zip or ``.fodt``
                    flat XML).
    :param src_format: ``"odt"`` (zipped OpenDocument, default) or ``"fodt"``
                    (flat XML).
    :return:        compiled template bytes in the same format as the input.
    :raises PreprocessError:
                    if the source contains fatal lint issues (unbalanced
                    directives, malformed grammar, un-parseable XML).
    """
    from .odt_io import load, dump

    content_xml, mimetype, extra_files = load(data, src_format)
    state = _State()
    try:
        new_content = _transform_content(content_xml, state)
    except etree.XMLSyntaxError as exc:
        raise PreprocessError(f"Source template contains invalid XML: {exc}") from exc

    errors = [i for i in state.issues if i.severity == "error"]
    if errors:
        joined = "; ".join(f"[{i.location}] {i.message}" for i in errors)
        raise PreprocessError(
            f"Refusing to compile: {len(errors)} error(s). First: {errors[0].message}",
            state.issues,
        )

    return dump(new_content, src_format, mimetype, extra_files)


def preprocess_file(source_path: str, target_path: str | None = None) -> str:
    """Convenience: compile a source file to a target file.

    :return: the path written (``target_path`` or the source path with
             ``.src`` stripped from its extension).
    """
    from pathlib import Path

    p = Path(source_path)
    raw = p.read_bytes()
    src_format = "fodt" if p.suffix == ".fodt" else "odt"
    out = preprocess(raw, src_format)
    if target_path is None:
        # ``foo.src.odt``  -> ``foo.odt``; ``foo.src.fodt`` -> ``foo.fodt``.
        name = p.name
        if ".src." in name:
            target = p.with_name(name.replace(".src.", "."))
        else:
            target = p.with_suffix(".compiled" + p.suffix)
    else:
        target = Path(target_path)
    target.write_bytes(out)
    return str(target)


def lint(data: bytes, src_format: str = "odt") -> list[Issue]:
    """Return diagnostics for a source template without writing a compiled one."""
    from .odt_io import load

    content_xml, _mt, _extra = load(data, src_format)
    state = _State()
    try:
        _transform_content(content_xml, state)
    except etree.XMLSyntaxError as exc:
        return [Issue("error", f"Source template contains invalid XML: {exc}", "")]
    return state.issues


# ---------------------------------------------------------------------------
# Transformation
# ---------------------------------------------------------------------------


def _transform_content(content_xml: bytes, state: _State) -> bytes:
    """Walk the content.xml tree, replacing tokens with py3o markup.

    Returns the new content.xml bytes.
    """
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.fromstring(content_xml, parser=parser)
    _transform_tree(tree, state)
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8")


def _transform_tree(root: etree._Element, state: _State) -> None:
    body = _find_body(root)
    if body is None:
        raise PreprocessError(
            "Template has no <office:body> — is this a valid OpenDocument?"
        )

    # Two passes so that decls collected during inline substitution are
    # written before the tree is re-serialized.

    # 1. Block-rule: paragraphs that are *only* a single directive become a
    #    py3o hyperlink element inside that paragraph (all other children/text
    #    dropped). This catches the common Liquid-style usage:
    #        <text:p>{% for x in xs %}</text:p>
    #    Block-rules run BEFORE inline replacement because we inspect the
    #    paragraph's full concatenated text, and any inline ``{{ }}`` present
    #    inside such a "directive-only" paragraph is itself an error (the
    #    author meant for the directive to sit alone).
    for paragraph in list(body.iter(_qn("text", "p"))) + list(body.iter(_qn("text", "h"))):
        _handle_block_directive_paragraph(paragraph, state)

    # 2. Inline replacement: ``{{ }}``/``{% %}``/``{# #}`` inside .text and
    #    .tail strings of every element in the body.
    for element in list(body.iter()):
        if _is_text_node_carrier(element):
            _handle_inline_tokens(element, "text", state)
        # tail belongs to the element's previous sibling context; handled via
        # the parent walk below.
    for element in list(body.iter()):
        _handle_inline_tokens(element, "tail", state)

    # 3. Forward block-direction balance check (raise lint issues for
    #    unterminated ``{% for %}``/``{% if %}``).
    _check_directive_balance(body, state)

    # 4. Emit collected user-field declarations.
    _emit_user_field_decls(body, state)


def _find_body(root: etree._Element) -> etree._Element | None:
    body = root.find(_qn("office", "body"))
    return body


def _is_text_node_carrier(_el: etree._Element) -> bool:
    """All elements can carry ``.text``; we only restrict via the iter walk."""
    return True


# ---------------------------------------------------------------------------
# Block directive handling
# ---------------------------------------------------------------------------

_PRINT_STYLE_HINT = "Internet_20_link"


def _handle_block_directive_paragraph(paragraph: etree._Element, state: _State) -> None:
    text = "".join(paragraph.itertext())
    token = paragraph_is_sole_directive(text)
    if token is None:
        return
    try:
        instr, _value, parsed = directive_parts(token.body)
    except ValueError as exc:
        state.issues.append(
            Issue("error", str(exc), _paragraph_location(paragraph))
        )
        return

    # A directive-only paragraph must be a control directive (for / if / elif /
    # else / endif / endfor / function). {{ }} and {# #} are not allowed here
    # because the block rule kicks in only when the whole paragraph content
    # starts with ``{%`` and ends with ``%}``.
    payload = _directive_payload(instr, parsed, token.body)
    if payload is None:
        # Treat as info — paragraph looked like a directive but wasn't.
        state.issues.append(
            Issue(
                "info",
                f"Paragraph contains a non-control directive token ({token.body}); treated as inline.",
                _paragraph_location(paragraph),
            )
        )
        return

    # Drop all current children (text content + element children).
    for child in list(paragraph):
        paragraph.remove(child)
    paragraph.text = None

    link = _make_py3o_link(payload, state)
    paragraph.append(link)


def _directive_payload(instr: str, parsed: dict[str, str], raw_body: str) -> str | None:
    """Translate a directive's parsed form into the py3o link payload.

    Returns ``None`` for tokens that are not control directives.
    """
    if instr == "for":
        return f"for={parsed['target']} in {parsed['iter']}"
    if instr == "if":
        return f"if={parsed['cond']}"
    if instr == "endif":
        return "/if"
    if instr == "endfor":
        return "/for"
    if instr == "function":
        return f'function="{parsed["value"]}"'
    # ``else``/``elif`` are refused earlier by ``directive_parts``; this branch
    # is unreachable in normal flow and serves as a defensive fallback.
    return None


def _make_py3o_link(payload: str, state: _State) -> etree._Element:
    """Build a ``<text:a xlink:href="py3o://<payload>">payload</text:a>`` element.

    The payload text is used both as the visible text (so py3o's
    ``validate_link`` sees the link text equal the URL payload) and — URL-safe
    — as the href after ``py3o://``.
    """
    # Closing markers carry a leading slash and use the three-slash form
    # ``py3o:///for``. py3o_base = statement[7:] strips just ``py3o://`` and
    # keeps the rest, including the leading ``/`` of a closing marker. Easy
    # rule: if payload starts with ``/`` (i.e. it's a closing) use the
    # three-slash form; otherwise two.
    if payload.startswith("/"):
        href = "py3o:///" + payload.lstrip("/")
    else:
        href = "py3o://" + urllib.parse.quote(payload, safe="=(),\"' ")
    link = etree.Element(
        _qn("text", "a"),
        attrib={
            _qn("xlink", "type"): "simple",
            _qn("xlink", "href"): href,
        },
        nsmap={"xlink": NS["xlink"]},
    )
    link.text = payload
    return link


# ---------------------------------------------------------------------------
# Inline token handling
# ---------------------------------------------------------------------------


def _handle_inline_tokens(element: etree._Element, attr: str, state: _State) -> None:
    """Replace ``{{ }}``/``{% %}``/``{# #}`` tokens inside ``element.<attr>``.

    ``attr`` is ``"text"`` (the element's leading text) or ``"tail"`` (text
    following a child element). The function splices the original string into
    pre-token text + emitted-element-node + post-token text across multiple
    tokens.
    """
    text = getattr(element, attr)
    if not text:
        return

    tokens = tokenize(text)
    if not tokens:
        return

    # Build a list of (kind, content) chunks: 'text' chunks carry literal
    # text; 'element' chunks carry an lxml element to insert as a sibling.
    chunks: list[tuple[str, object]] = []
    cursor = 0
    for tok in tokens:
        if tok.start > cursor:
            chunks.append(("text", text[cursor : tok.start]))
        if tok.kind == "comment":
            # Drop entirely; do not append an element chunk.
            pass
        elif tok.kind == "var":
            el = _make_user_field_get(tok.body.strip(), state)
            if el is not None:
                chunks.append(("element", el))
        else:  # directive token inline.
            el = _make_inline_directive_element(tok, state)
            if el is not None:
                chunks.append(("element", el))
        cursor = tok.end
    if cursor < len(text):
        chunks.append(("text", text[cursor:]))

    # Early-exit optimization: if no substitutions / directives were emitted
    # (i.e. only comments — or no tokens at all), still rewrite the text so
    # comments are stripped. Tokens-only-no-changes is handled at the
    # function's top-level early-exit (``if not tokens: return``).
    has_element = any(c[0] == "element" for c in chunks)
    if not has_element:
        # Rebuild text by concatenating only ``text`` chunks; comments drop.
        rebuilt = "".join(c[1] for c in chunks if c[0] == "text")
        setattr(element, attr, rebuilt)
        return

    first_text = ""
    first_text_set = False
    last_elem: etree._Element | None = None
    pending_text = ""

    for kind, content in chunks:
        if kind == "text":
            if not first_text_set:
                first_text += content
            else:
                pending_text += content
        else:  # element
            if not first_text_set:
                # First element: emit leading text first.
                setattr(element, attr, first_text)
                first_text_set = True
            else:
                if last_elem is not None:
                    last_elem.tail = pending_text
                pending_text = ""
            # Insert the element relative to its anchor.
            _attach_element(element, attr, content)
            last_elem = content

    # Trailing text after the last element becomes its tail.
    if last_elem is not None:
        last_elem.tail = pending_text

    # If every chunk was 'text' (comments only), update the attribute.
    if not first_text_set:
        setattr(element, attr, first_text)


def _attach_element(parent: etree._Element, attr: str, child: etree._Element) -> None:
    """Insert ``child`` immediately after the appropriate anchor.

    For ``attr == "text"`` the child becomes a direct child of ``parent``
    (appended after existing children, but conceptually it follows the
    paragraph's leading text).

    For ``attr == "tail"`` the child is inserted after ``parent`` as a sibling
    (in parent's parent).
    """
    if attr == "text":
        parent.append(child)
    else:
        grand = parent.getparent()
        if grand is not None:
            grand.insert(grand.index(parent) + 1, child)


def _make_user_field_get(expr: str, state: _State) -> etree._Element | None:
    """Build a ``<text:user-field-get text:name="py3o.<expr>"/>`` element.

    Registers the variable name with a hint so the declarations block can be
    populated at the end.

    Sets ``office:value-type="string"`` and ``office:string-value="<expr>"``
    so that the variable's placeholder text in LibreOffice shows the
    expression itself (helpful for the author who opens the compiled .odt).
    """
    expr = expr.strip()
    if not expr:
        state.issues.append(
            Issue("error", "Empty {{ }} expression", "inline")
        )
        return None
    # Validate the expression is a Python expression (catches typos early).
    try:
        ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        state.issues.append(
            Issue(
                "warning",
                f"Expression {expr!r} is not valid Python: {exc.msg}",
                "inline",
            )
        )
    name = f"py3o.{expr}"
    state.user_fields.setdefault(name, expr)  # placeholder hint
    return etree.Element(
        _qn("text", "user-field-get"),
        attrib={_qn("text", "name"): name},
        nsmap={"text": NS["text"]},
    )


def _make_inline_directive_element(token: Token, state: _State) -> etree._Element | None:
    """Build a ``<text:a>`` element for an inline directive token.

    Inline directives are rarer — the natural form is the block rule. When
    an author writes ``{% if x %}`` mid-paragraph (inline), we still produce
    the same ``<text:a>`` element; py3o will relocate it via its own
    ``handle_link`` machinery.
    """
    try:
        instr, _value, parsed = directive_parts(token.body)
    except ValueError as exc:
        state.issues.append(Issue("error", str(exc), "inline"))
        return None
    payload = _directive_payload(instr, parsed, token.body)
    if payload is None:
        state.issues.append(
            Issue("error", f"Cannot translate directive inline: {token.body!r}", "inline")
        )
        return None
    return _make_py3o_link(payload, state)


# ---------------------------------------------------------------------------
# Directive balance
# ---------------------------------------------------------------------------


def _check_directive_balance(body: etree._Element, state: _State) -> None:
    """Walk all emitted py3o links; report unbalanced ``for``/``if`` blocks."""
    stack: list[tuple[str, etree._Element]] = []
    for link in body.iter(_qn("text", "a")):
        href = link.get(_qn("xlink", "href")) or ""
        if not href.startswith("py3o://"):
            continue
        payload = urllib.parse.unquote(href[7:])  # strip py3o://
        # Closings look like ``/for`` / ``/if``; openings look like
        # ``for=...`` / ``if=...``/``elif=...``/``else``.
        if payload == "/for" or payload == "/if":
            expected_kind = payload[1:]
            if not stack or stack[-1][0] != expected_kind:
                state.issues.append(
                    Issue(
                        "error",
                        f"Unmatched closing {payload!r}; no matching open directive.",
                        _paragraph_location(link),
                    )
                )
                continue
            stack.pop()
        elif payload.startswith("for=") or payload.startswith("if="):
            stack.append(("for" if payload.startswith("for=") else "if", link))
        # elif / else are mid-block; they don't change the stack.
    for kind, link in stack:
        state.issues.append(
            Issue(
                "error",
                f"Unbalanced directive: {{% {kind} ... %}} never closed.",
                _paragraph_location(link),
            )
        )


def _paragraph_location(el: etree._Element) -> str:
    """A short XPath-ish location string for diagnostics."""
    parent = el.getparent()
    if parent is None:
        return "root"
    try:
        idx = list(parent).index(el) + 1
    except ValueError:
        idx = 0
    return f"{parent.tag}/{el.tag}[{idx}]"


# ---------------------------------------------------------------------------
# User-field declarations
# ---------------------------------------------------------------------------


def _emit_user_field_decls(body: etree._Element, state: _State) -> None:
    """Append a ``<text:user-field-decls>`` block with one declaration per
    collected variable.

    Existing declarations are preserved (we don't remove ones the author
    already authored by hand in the source — that's a legitimate escape hatch).
    """
    if not state.user_fields:
        return
    # Find the body text/spreadsheet container.
    container = body.find(_qn("office", "text"))
    if container is None:
        container = body.find(_qn("office", "spreadsheet"))
    if container is None:
        # Other body types (presentation, draw) — keep the decls but warn.
        return

    decls_block = container.find(_qn("text", "user-field-decls"))
    existing_names: set[str] = set()
    if decls_block is None:
        decls_block = etree.Element(_qn("text", "user-field-decls"), nsmap={"text": NS["text"]})
        container.insert(0, decls_block)
    else:
        for decl in decls_block.findall(_qn("text", "user-field-decl")):
            name = decl.get(_qn("text", "name"))
            if name:
                existing_names.add(name)

    for name, hint in state.user_fields.items():
        if name in existing_names:
            continue
        decl = etree.SubElement(
            decls_block,
            _qn("text", "user-field-decl"),
            attrib={
                _qn("office", "value-type"): "string",
                _qn("office", "string-value"): hint,
                _qn("text", "name"): name,
            },
            nsmap={"text": NS["text"], "office": NS["office"]},
        )
        # Reorder: lxml's SubElement appends at end which is fine.
        _ = decl  # silence unused-variable linters
