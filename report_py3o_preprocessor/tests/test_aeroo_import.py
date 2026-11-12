# Copyright 2026 Open Eye Development
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Tests for the Aeroo → py3o-preprocessor source converter.

Run directly (no pytest needed)::

    python tests/test_aeroo_import.py

Expects ``ALL TESTS PASSED`` at the end. Tests exercise the converter against
synthetic minimal ``.odt`` zips carrying ``<text:placeholder>`` elements with
Aeroo/Genshi expressions, then feed the converted output to the preprocessor
to confirm the whole pipeline (Aeroo → source → compiled py3o) is sound.
"""
import io
import sys
import textwrap
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers.aeroo_import import convert_aeroo, convert_aeroo_file  # noqa: E402
from helpers.preprocessor import lint, preprocess  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal .odt builder with placeholders
# ---------------------------------------------------------------------------

def _build_minimal_odt(content_xml_body: str) -> bytes:
    """A tiny but valid .odt zip carrying ``content_xml_body`` as content.xml."""
    mimetype = b"application/vnd.oasis.opendocument.text"
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest='
        '"urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">'
        '<manifest:file-entry manifest:media-type='
        '"application/vnd.oasis.opendocument.text" manifest:full-path="/"/>'
        '<manifest:file-entry manifest:media-type="text/xml" manifest:full-path="content.xml"/>'
        '<manifest:file-entry manifest:media-type="text/xml" manifest:full-path="styles.xml"/>'
        '</manifest:manifest>'
    ).encode("utf-8")
    styles = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-styles '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'office:version="1.2">'
        '<office:styles>'
        '<style:style style:name="Standard" style:family="paragraph"/>'
        '</office:styles></office:document-styles>'
    ).encode("utf-8")
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'office:version="1.2">'
        '<office:body><office:text>'
        + content_xml_body
        + '</office:text></office:body></office:document-content>'
    ).encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        mt = zipfile.ZipInfo("mimetype")
        mt.compress_type = zipfile.ZIP_STORED
        zf.writestr(mt, mimetype)
        for name, raw in [
            ("META-INF/manifest.xml", manifest),
            ("styles.xml", styles),
            ("content.xml", content),
        ]:
            zi = zipfile.ZipInfo(name)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, raw)
    return out.getvalue()


def _ph(expr: str) -> str:
    """Build a ``<text:placeholder>`` element whose (escaped) text is ``<expr>``."""
    from html import escape

    wrapped = "<" + expr + ">"
    return f'<text:placeholder text:placeholder-type="text">{escape(wrapped)}</text:placeholder>'


def _extract_content_xml(odt_bytes: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(odt_bytes), "r") as zf:
        return zf.read("content.xml")


def _parse_links(out: bytes):
    """Return list of (unquoted_href, visible_text) for every <text:a>."""
    from urllib.parse import unquote

    from lxml import etree

    tree = etree.fromstring(out)
    ns = {
        "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
        "xlink": "http://www.w3.org/1999/xlink",
    }
    pairs = []
    for a in tree.iter("{%s}a" % ns["text"]):
        href = a.get("{%s}href" % ns["xlink"]) or ""
        pairs.append((unquote(href), a.text or ""))
    return pairs


def _parse_user_field_gets(out: bytes) -> list[str]:
    from lxml import etree

    tree = etree.fromstring(out)
    ns = {"text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}
    return [el.get("{%s}name" % ns["text"]) for el in tree.iter("{%s}user-field-get" % ns["text"])]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_variable_substitution():
    """A plain ``<objects[0].name>`` becomes ``{{ objects[0].name }}`` and
    survives the full pipeline as a py3o user-field-get."""
    src = _build_minimal_odt(f'<text:p>Name: {_ph("objects[0].name")}.</text:p>')
    converted, issues = convert_aeroo(src, "odt")
    assert not [i for i in issues if i.severity == "error"], issues
    content = _extract_content_xml(converted)
    # No leftover <text:placeholder> in the converted output.
    assert b"<text:placeholder" not in content
    # The Jinja token sits in a span, visible in the XML.
    assert b"{{ objects[0].name }}" in content
    # Full pipeline → compiled py3o user-field.
    compiled = preprocess(converted, "odt")
    names = _parse_user_field_gets(_extract_content_xml(compiled))
    assert "py3o.objects[0].name" in names, names
    print("test_variable_substitution OK")


def test_o_alias_rewrite():
    """``o.field`` is mechanically rewritten to ``objects[0].field`` with an
    info-level lint entry."""
    src = _build_minimal_odt(f'<text:p>{_ph("o.project_id.name")}</text:p>')
    converted, issues = convert_aeroo(src, "odt")
    content = _extract_content_xml(converted)
    assert b"{{ objects[0].project_id.name }}" in content, content
    assert any(
        "o." in i.message and "objects[0]." in i.message and i.severity == "info"
        for i in issues
    ), issues
    # Full pipeline → compiles cleanly.
    compiled = preprocess(converted, "odt")
    names = _parse_user_field_gets(_extract_content_xml(compiled))
    assert "py3o.objects[0].project_id.name" in names, names
    print("test_o_alias_rewrite OK")


def test_o_alias_not_overmatching():
    """The ``o.`` rewrite must NOT touch ``objects[0].`` or ``foo.o.x``."""
    src = _build_minimal_odt(
        f'<text:p>{_ph("objects[0].name")}</text:p>'
        f'<text:p>{_ph("foo.o.x")}</text:p>'
    )
    converted, issues = convert_aeroo(src, "odt")
    content = _extract_content_xml(converted)
    # ``objects[0].name`` stays as-is (no double rewrite).
    assert b"{{ objects[0].name }}" in content
    # ``foo.o.x`` is treated as a bare expression and preserved verbatim
    # (the rewrite regex uses a negative lookbehind on identifier chars).
    assert b"{{ foo.o.x }}" in content, content
    print("test_o_alias_not_overmatching OK")


def test_for_each_to_for():
    """``<for each="x in xs">`` → ``{% for x in xs %}`` (block-rule paragraph)
    compiles into a py3o for-link."""
    body = (
        f'<text:p>{_ph("for each=\"x in xs\"")}</text:p>'
        '<text:p>{{ x.name }}</text:p>'
        f'<text:p>{_ph("/for")}</text:p>'
    )
    src = _build_minimal_odt(body)
    converted, issues = convert_aeroo(src, "odt")
    content = _extract_content_xml(converted)
    assert b"{% for x in xs %}" in content, content
    assert b"{% endfor %}" in content, content
    # Full pipeline → py3o for-link + closing.
    compiled = preprocess(converted, "odt")
    pairs = _parse_links(_extract_content_xml(compiled))
    assert ("py3o://for=x in xs", "for=x in xs") in pairs, pairs
    assert ("py3o:///for", "/for") in pairs, pairs
    print("test_for_each_to_for OK")


def test_for_each_o_alias_in_iterable():
    """``<for each="x in o.items">`` rewrites the ``o.`` in the iterable to
    ``objects[0].`` but leaves the loop target ``x`` untouched."""
    src = _build_minimal_odt(
        f'<text:p>{_ph("for each=\"x in o.items\"")}</text:p>'
        '<text:p>{{ x.name }}</text:p>'
        f'<text:p>{_ph("/for")}</text:p>'
    )
    converted, issues = convert_aeroo(src, "odt")
    content = _extract_content_xml(converted)
    from lxml import etree

    tree = etree.fromstring(content)
    ns = {"text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}
    spans = [s.text for s in tree.iter("{%s}span" % ns["text"])]
    assert "{% for x in objects[0].items %}" in spans, spans
    assert any(
        "for-each iterable" in i.message and i.severity == "info" for i in issues
    ), issues
    compiled = preprocess(converted, "odt")
    pairs = _parse_links(_extract_content_xml(compiled))
    assert ("py3o://for=x in objects[0].items", "for=x in objects[0].items") in pairs, pairs
    print("test_for_each_o_alias_in_iterable OK")


def test_if_test_to_if():
    """``<if test="cond">`` / ``</if>`` → ``{% if cond %}`` / ``{% endif %}``."""
    body = (
        f'<text:p>{_ph("if test=\"x &gt; 0\"")}</text:p>'
        '<text:p>positive</text:p>'
        f'<text:p>{_ph("/if")}</text:p>'
    )
    src = _build_minimal_odt(body)
    converted, issues = convert_aeroo(src, "odt")
    content = _extract_content_xml(converted)
    # lxml escapes ``>`` as ``&gt;`` in text serialization; parse the span
    # text to see the token as the author will.
    from lxml import etree

    tree = etree.fromstring(content)
    ns = {"text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}
    spans = [s.text for s in tree.iter("{%s}span" % ns["text"])]
    assert "{% if x > 0 %}" in spans, spans
    assert "{% endif %}" in spans, spans
    compiled = preprocess(converted, "odt")
    pairs = _parse_links(_extract_content_xml(compiled))
    assert ("py3o://if=x > 0", "if=x > 0") in pairs, pairs
    assert ("py3o:///if", "/if") in pairs, pairs
    print("test_if_test_to_if OK")


def test_setlang_drops_to_comment():
    """``<setLang('en_GB')>`` is dropped and replaced with a {# … #} comment
    that the preprocessor strips at compile time."""
    src = _build_minimal_odt(
        f'<text:p>{_ph("setLang(\'en_GB\')")}</text:p>'
        '<text:p>hello</text:p>'
    )
    converted, issues = convert_aeroo(src, "odt")
    content = _extract_content_xml(converted)
    assert b"{# setLang" in content, content
    assert b"py3o has no setLang" in content, content
    # The comment must NOT survive into the compiled py3o output.
    compiled = preprocess(converted, "odt")
    compiled_content = _extract_content_xml(compiled)
    assert b"setLang" not in compiled_content, compiled_content
    print("test_setlang_drops_to_comment OK")


def test_formatlang_rename():
    """``<formatLang(d.amount)>`` → ``{{ o_format_lang(d.amount) }}``."""
    src = _build_minimal_odt(f'<text:p>{_ph("formatLang(d.amount)")}</text:p>')
    converted, issues = convert_aeroo(src, "odt")
    content = _extract_content_xml(converted)
    assert b"{{ o_format_lang(d.amount) }}" in content, content
    compiled = preprocess(converted, "odt")
    names = _parse_user_field_gets(_extract_content_xml(compiled))
    assert "py3o.o_format_lang(d.amount)" in names, names
    print("test_formatlang_rename OK")


def test_double_bracket_typo():
    """``<<objects[0].name>>`` (extra brackets) is flattened to
    ``{{ objects[0].name }}`` with an info-level diagnostic."""
    # Build a placeholder whose text is ``&lt;&lt;objects[0].name&gt;&gt;``.
    from html import escape

    raw = "<<objects[0].name>>"
    ph = f'<text:placeholder text:placeholder-type="text">{escape(raw)}</text:placeholder>'
    src = _build_minimal_odt(f'<text:p>{ph}</text:p>')
    converted, issues = convert_aeroo(src, "odt")
    content = _extract_content_xml(converted)
    assert b"{{ objects[0].name }}" in content, content
    assert any("Stray double-bracket" in i.message and i.severity == "info" for i in issues), issues
    print("test_double_bracket_typo OK")


def test_choose_refused():
    """Aeroo ``<choose>``/``<when test=…>``/``<otherwise>`` have no py3o
    equivalent → the converter refuses them with a warning and leaves the
    placeholder in place for manual refactoring."""
    for aeroo in ("choose", "otherwise"):
        src = _build_minimal_odt(f'<text:p>{_ph(aeroo)}</text:p>')
        converted, issues = convert_aeroo(src, "odt")
        content = _extract_content_xml(converted)
        # Placeholder preserved so the author sees what to fix.
        assert b"<text:placeholder" in content, (aeroo, content)
        assert any(
            "no py3o equivalent" in i.message and i.severity == "warning"
            for i in issues
        ), (aeroo, issues)
    # ``when test="cond"`` same treatment.
    src = _build_minimal_odt(f'<text:p>{_ph("when test=\"cond\"")}</text:p>')
    converted, issues = convert_aeroo(src, "odt")
    content = _extract_content_xml(converted)
    assert b"<text:placeholder" in content, content
    print("test_choose_refused OK")


def test_preserves_surrounding_text_and_tail():
    """The placeholder's tail text and sibling structure survive the rewrite."""
    body = (
        f'<text:p>Before {_ph("objects[0].name")} after.</text:p>'
        f'<text:p>Second paragraph.</text:p>'
    )
    src = _build_minimal_odt(body)
    converted, _ = convert_aeroo(src, "odt")
    content = _extract_content_xml(converted)
    assert b"Before " in content
    assert b" after." in content
    assert b"Second paragraph." in content
    print("test_preserves_surrounding_text_and_tail OK")


def test_file_roundtrip(tmp_path=None):
    """``convert_aeroo_file`` writes a ``.src.odt`` next to the source."""
    import tempfile

    workdir = tmp_path or Path(tempfile.mkdtemp(prefix="aeroo_test_"))
    src_path = workdir / "report.odt"
    src_bytes = _build_minimal_odt(
        f'<text:p>{_ph("objects[0].name")}</text:p>'
        f'<text:p>{_ph("for each=\"x in xs\"")}</text:p>'
        '<text:p>{{ x.title }}</text:p>'
        f'<text:p>{_ph("/for")}</text:p>'
    )
    src_path.write_bytes(src_bytes)
    written, issues = convert_aeroo_file(str(src_path))
    assert written.endswith(".src.odt"), written
    out_bytes = Path(written).read_bytes()
    content = _extract_content_xml(out_bytes)
    assert b"{{ objects[0].name }}" in content
    assert b"{% for x in xs %}" in content
    # And the full pipeline compiles.
    compiled = preprocess(out_bytes, "odt")
    pairs = _parse_links(_extract_content_xml(compiled))
    assert ("py3o://for=x in xs", "for=x in xs") in pairs, pairs
    assert ("py3o:///for", "/for") in pairs, pairs
    print("test_file_roundtrip OK")


def main():
    test_variable_substitution()
    test_o_alias_rewrite()
    test_o_alias_not_overmatching()
    test_for_each_to_for()
    test_for_each_o_alias_in_iterable()
    test_if_test_to_if()
    test_setlang_drops_to_comment()
    test_formatlang_rename()
    test_double_bracket_typo()
    test_choose_refused()
    test_preserves_surrounding_text_and_tail()
    test_file_roundtrip()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
