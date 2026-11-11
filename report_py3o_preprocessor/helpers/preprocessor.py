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

    # 2. Inline replacement: walk every paragraph (``<text:p>`` / ``<text:h>``)
    #    and operate on the *flattened* text of the whole paragraph, not the
    #    per-element ``.text``/``.tail`` strings. This is intentional and
    #    necessary: LibreOffice frequently inserts ``<text:span>`` formatting
    #    runs *inside* a token the author typed — e.g. marking up an
    #    autocorrected ``_`` or a spell-check suggestion — which would split
    #    a ``{{ create_date }}`` token across multiple XML elements and break
    #    any per-element tokenizer. By concatenating the paragraph's full text
    #    first, we see the tokens as the author intended. The cost is that
    #    formatting runs *inside* a token (which carry no semantic meaning for
    #    a substituted value anyway) are flattened; formatting on literal
    #    text around tokens is preserved (paragraph is rewritten only when
    #    tokens are present).
    for paragraph in list(body.iter(_qn("text", "p"))) + list(body.iter(_qn("text", "h"))):
        _handle_inline_paragraph(paragraph, state)

    # 3. Forward block-direction balance check (raise lint issues for
    #    unterminated ``{% for %}``/``{% if %}``).
    _check_directive_balance(body, state)

    # 4. Emit collected user-field declarations.
    _emit_user_field_decls(body, state)


def _find_body(root: etree._Element) -> etree._Element | None:
    body = root.find(_qn("office", "body"))
    return body


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
    # NOTE: no ``nsmap=`` — see comment in ``_make_user_field_get``. The
    # xlink namespace is declared on the parent tree (parsed from the
    # original .odt) and inherited automatically.
    link = etree.Element(
        _qn("text", "a"),
        attrib={
            _qn("xlink", "type"): "simple",
            _qn("xlink", "href"): href,
        },
    )
    link.text = payload
    return link


# ---------------------------------------------------------------------------
# Inline token handling (paragraph-level)
# ---------------------------------------------------------------------------


def _handle_inline_paragraph(paragraph: etree._Element, state: _State) -> None:
    """Replace ``{{ }}``/``{% %}``/``{# #}`` tokens inside a paragraph.

    Operates on the paragraph's *flattened* text (concatenation of ``.text``,
    each child's ``.text`` and ``.tail``) rather than per-element ``.text``/
    ``.tail`` strings. This is necessary because LibreOffice inserts
    ``<text:span>`` formatting runs in the middle of a token the author
    typed (autocorrect, spell-checking,IME composition, ...) and a
    per-element tokenizer would see broken-up tokens it cannot recognise.

    When tokens are present in a paragraph, the paragraph's children are
    cleared and rebuilt as: literal-text-children (no internal styling —
    spans inside a token lose their formatting, which is semantically
    correct since py3o user-field substitution produces a single value
    with no internal styling anyway) interleaved with emitted element
    nodes (``<text:user-field-get>`` / ``<text:a>``).

    Paragraphs with no tokens are left untouched, preserving the author's
    formatting verbatim.

    Tokens that span multiple paragraphs (``{{`` opened in one paragraph
    without a matching ``}}``) are reported as errors at lint time — the
    author must keep each token within a single paragraph.
    """
    full_text = "".join(paragraph.itertext())
    if not full_text:
        return
    tokens = tokenize(full_text)
    if not tokens:
        return  # No tokens: leave formatting untouched.

    # Build a flat chunk list: ("text", literal) and ("element", lxml).
    chunks: list[tuple[str, object]] = []
    cursor = 0
    had_token_breaking_internal_span = False
    for tok in tokens:
        if tok.start > cursor:
            chunks.append(("text", full_text[cursor : tok.start]))
        if tok.kind == "comment":
            # Drop entirely.
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
    if cursor < len(full_text):
        chunks.append(("text", full_text[cursor:]))

    # If only text survived (comments-only or all-var-rejected), still
    # rebuild so the comments are stripped.
    has_element = any(c[0] == "element" for c in chunks)
    if has_element:
        # Detect whether the original paragraph had any child elements
        # *inside* a token range — i.e. spans that were flattened. This is
        # informational; the author may want to know.
        if _paragraph_had_spans_inside_tokens(paragraph, tokens):
            state.issues.append(
                Issue(
                    "info",
                    "Formatting runs inside a token were flattened during "
                    "compilation. The substituted value will inherit the "
                    "paragraph's base style.",
                    _paragraph_location(paragraph),
                )
            )
        # Clear the paragraph and rebuild.
        for child in list(paragraph):
            paragraph.remove(child)
        paragraph.text = None

        # Reconstruct: first leading text becomes paragraph.text; subsequent
        # text becomes the tail of the last appended element.
        first_text = ""
        first_elem_seen = False
        last_elem: etree._Element | None = None
        pending_text = ""
        for kind, content in chunks:
            if kind == "text":
                if not first_elem_seen:
                    first_text += content  # type: ignore[operator]
                else:
                    pending_text += content  # type: ignore[operator]
            else:  # element
                if not first_elem_seen:
                    paragraph.text = first_text
                    first_elem_seen = True
                else:
                    if last_elem is not None:
                        last_elem.tail = pending_text
                    pending_text = ""
                paragraph.append(content)
                last_elem = content
        if last_elem is not None:
            last_elem.tail = pending_text
        elif not first_elem_seen:
            # Only comments survived; emit whatever text we rebuilt.
            paragraph.text = first_text
    else:
        # Only comments survived (every var/directive token rejected) —
        # rebuild text without comments. This still flattens spans, but
        # we'll only do this if comments were actually stripped.
        rebuilt = "".join(c[1] for c in chunks if c[0] == "text")
        if rebuilt != full_text:
            for child in list(paragraph):
                paragraph.remove(child)
            paragraph.text = rebuilt


def _paragraph_had_spans_inside_tokens(
    paragraph: etree._Element, tokens: list
) -> bool:
    """Return True iff any child element of ``paragraph`` existed within the
    character range of a token (i.e. a span was flattened into a token).

    This is a heuristic: we look at the paragraph's children and check
    whether any of them appears between the start and end of a token in
    the flattened text. The check is conservative; false positives are
    acceptable for an informational issue.
    """
    if len(paragraph) == 0:
        return False
    # Reconstruct (offset, is_in_token) ranges by walking the paragraph in
    # document order with running offsets.
    try:
        offset = 0
        ranges = []  # list of (start, end, is_text) for each piece
        if paragraph.text:
            ranges.append((offset, offset + len(paragraph.text), True))
            offset += len(paragraph.text)
        for child in paragraph:
            if child.text:
                ranges.append((offset, offset + len(child.text), True))
                offset += len(child.text)
            # Element itself takes no characters in flat text.
            if child.tail:
                ranges.append((offset, offset + len(child.tail), True))
                offset += len(child.tail)
        # Now check whether any element position falls inside a token.
        char_pos = 0
        elem_positions = []
        if paragraph.text:
            char_pos += len(paragraph.text)
        for child in paragraph:
            elem_positions.append(char_pos)  # element starts here
            if child.text:
                char_pos += len(child.text)
            if child.tail:
                char_pos += len(child.tail)
        for tok in tokens:
            for ep in elem_positions:
                if tok.start < ep < tok.end:
                    return True
    except Exception:
        return False
    return False


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
    # NOTE: do NOT pass ``nsmap=`` here. lxml pre-5.x has a serialization
    # quirk where freshly-created elements with their own nsmap lose their
    # ``.tail`` text when serialized via ``etree.tostring`` — even though
    # the in-memory ``.tail`` attribute reads correctly. The element
    # inherits the parent's namespace bindings upon insertion, so an explicit
    # nsmap is redundant anyway.
    return etree.Element(
        _qn("text", "user-field-get"),
        attrib={_qn("text", "name"): name},
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
        # No nsmap (the parent tree already declares these prefixes; passing
        # an explicit nsmap would make lxml drop our .tail on serialization).
        decls_block = etree.Element(_qn("text", "user-field-decls"))
        container.insert(0, decls_block)
    else:
        for decl in decls_block.findall(_qn("text", "user-field-decl")):
            name = decl.get(_qn("text", "name"))
            if name:
                existing_names.add(name)

    for name, hint in state.user_fields.items():
        if name in existing_names:
            continue
        etree.SubElement(
            decls_block,
            _qn("text", "user-field-decl"),
            attrib={
                _qn("office", "value-type"): "string",
                _qn("office", "string-value"): hint,
                _qn("text", "name"): name,
            },
        )
