# Copyright 2026 Open Eye Development
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import sys
import textwrap
from pathlib import Path

# Allow running these tests directly with ``python test_preprocessor.py`` from
# the module root (no pytest/install needed) by inserting the parent dir so
# the ``helpers`` package imports cleanly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers.preprocessor import Issue, PreprocessError, lint, preprocess  # noqa: E402
from helpers.tokens import (  # noqa: E402
    paragraph_is_sole_directive,
    tokenize,
)

# Smallest legitimate fodt — just enough structure for the transformer.
# Parses cleanly with lxml and exposes an <office:body>/<office:text>.
MIN_FODT_TEMPLATE = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <office:document
        xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
        xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
        xmlns:xlink="http://www.w3.org/1999/xlink"
        office:version="1.2">
      <office:body>
        <office:text>
          <text:p>{tokens_var}</text:p>
          <text:p>{tokens_for_open}</text:p>
          <text:p>{tokens_loop_body}</text:p>
          <text:p>{tokens_for_close}</text:p>
          <text:p>{tokens_if}</text:p>
          <text:p>text inside if</text:p>
          <text:p>{tokens_endif}</text:p>
          <text:p>{tokens_function}</text:p>
          <text:p>{tokens_comment_inline}</text:p>
        </office:text>
      </office:body>
    </office:document>
    """
)


def _source():
    return MIN_FODT_TEMPLATE.format(
        tokens_var="Hello {{ user.name }}, welcome.",
        tokens_for_open="{% for line in lines %}",
        tokens_loop_body="Line: {{ line.title }}",
        tokens_for_close="{% endfor %}",
        tokens_if='{% if user.amount > 0 %}',
        tokens_endif="{% endif %}",
        tokens_function='{% function="format_currency(user.amount, \'EUR\')" %}',
        tokens_comment_inline="prefix {# hidden note #} suffix",
    ).encode("utf-8")


def _contains(haystack: bytes, needle: bytes) -> bool:
    return needle in haystack


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


def test_variable_substitution():
    out = preprocess(_source(), "fodt")
    names = _parse_user_field_gets(out)
    assert "py3o.user.name" in names
    assert "py3o.line.title" in names
    # Declaration block must contain both names.
    assert b'<text:user-field-decl ' in out
    assert b'text:name="py3o.user.name"' in out
    assert b'text:name="py3o.line.title"' in out
    # Plain text around the var is preserved.
    assert b"Hello " in out and b", welcome." in out
    print("test_variable_substitution OK")


def test_forloop_emit():
    out = preprocess(_source(), "fodt")
    pairs = _parse_links(out)
    assert ("py3o://for=line in lines", "for=line in lines") in pairs, pairs
    assert ("py3o:///for", "/for") in pairs, pairs
    print("test_forloop_emit OK")


def test_if_emit():
    out = preprocess(_source(), "fodt")
    pairs = _parse_links(out)
    assert ("py3o://if=user.amount > 0", "if=user.amount > 0") in pairs, pairs
    assert ("py3o:///if", "/if") in pairs, pairs
    print("test_if_emit OK")


def test_function_directive():
    out = preprocess(_source(), "fodt")
    pairs = _parse_links(out)
    # Function payload carries quotes; unquote is a no-op on the quoted URL.
    assert any(
        h == "py3o://function=\"format_currency(user.amount, 'EUR')\"" and t == "function=\"format_currency(user.amount, 'EUR')\""
        for h, t in pairs
    ), pairs
    print("test_function_directive OK")


def test_comments_are_dropped():
    out = preprocess(_source(), "fodt")
    assert b"hidden note" not in out
    # Surrounding text preserved.
    assert b"prefix " in out
    assert b" suffix" in out
    print("test_comments_are_dropped OK")


def test_lint_unbalanced_for():
    src = MIN_FODT_TEMPLATE.format(
        tokens_var="{{ x }}",
        tokens_for_open="{% for x in xs %}",
        tokens_loop_body="",
        tokens_for_close="",
        tokens_if="",
        tokens_endif="",
        tokens_function="",
        tokens_comment_inline="",
    ).encode("utf-8")
    issues = lint(src, "fodt")
    assert any("Unbalanced directive" in i.message and i.severity == "error" for i in issues)
    # And ``preprocess`` should refuse to compile.
    try:
        preprocess(src, "fodt")
    except PreprocessError as exc:
        assert exc.issues, "PreprocessError should carry issues"
    else:  # pragma: no cover
        raise AssertionError("Unbalanced template should raise PreprocessError")
    print("test_lint_unbalanced_for OK")


def test_lint_refuses_elif():
    src = MIN_FODT_TEMPLATE.format(
        tokens_var="{% elif x %}",
        tokens_for_open="",
        tokens_loop_body="",
        tokens_for_close="",
        tokens_if="{% if a %}",
        tokens_endif="{% endif %}",
        tokens_function="",
        tokens_comment_inline="",
    ).encode("utf-8")
    issues = lint(src, "fodt")
    assert any(
        "py3o.template does not support 'elif'" in i.message and i.severity == "error"
        for i in issues
    ), issues
    print("test_lint_refuses_elif OK")


def test_tokenizer_basic():
    toks = tokenize("{{ x }} {% for y in ys %} {# c #}")
    assert len(toks) == 3
    assert toks[0].kind == "var" and toks[0].body == "x"
    assert toks[1].kind == "directive" and toks[1].body == "for y in ys"
    assert toks[2].kind == "comment" and toks[2].body == "c"
    print("test_tokenizer_basic OK")


def test_block_rule_recognises_sole_directive():
    assert paragraph_is_sole_directive("{% for x in xs %}") is not None
    assert paragraph_is_sole_directive("  {% for x in xs %}  ") is not None
    # Text alongside the directive disqualifies block rule.
    assert paragraph_is_sole_directive("hello {% for x in xs %}") is None
    # Variable substitution is never a block directive.
    assert paragraph_is_sole_directive("{{ x }}") is None
    print("test_block_rule_recognises_sole_directive OK")


def _build_minimal_odt(content_xml_body: str) -> bytes:
    """Build a tiny but valid ``.odt`` zip carrying ``content_xml_body`` as
    content.xml. Useful for tests that feed output directly to py3o.template."""
    import io
    import zipfile

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


def test_py3o_accepts_compiled_template():
    """End-to-end: build a minimal .odt zip with Jinja tokens, preprocess it,
    then feed the compiled .odt to ``py3o.template.Template`` and confirm the
    py3o parser accepts it without raising."""
    import shutil
    import tempfile
    from pathlib import Path

    body = (
        '<text:p>Hello {{ user.name }}, welcome.</text:p>'
        '<text:p>{% for line in lines %}</text:p>'
        '<text:p>Line: {{ line.title }}</text:p>'
        '<text:p>{% endfor %}</text:p>'
        '<text:p>{% if user.amount > 0 %}</text:p>'
        '<text:p>positive</text:p>'
        '<text:p>{% endif %}</text:p>'
        "<text:p>prefix {# hidden note #} suffix</text:p>"
    )
    src = _build_minimal_odt(body)
    compiled = preprocess(src, "odt")
    workdir = tempfile.mkdtemp(prefix="py3o_pre_")
    try:
        src_path = Path(workdir) / "hello.src.odt"
        compiled_path = Path(workdir) / "hello_compiled.odt"
        src_path.write_bytes(src)
        compiled_path.write_bytes(compiled)

        from py3o.template import Template

        out_path = compiled_path.with_suffix(".out.odt")
        template = Template(str(compiled_path), str(out_path))
        # Public seam exercising the same XML scan as the production render
        # path. Returns a list of raw py3o expressions extracted from the
        # template.
        expressions = template.get_all_user_python_expression()
        assert "user.name" in expressions, expressions
        assert "for=line in lines" in expressions, expressions
        assert "line.title" in expressions, expressions
        assert "/for" in expressions, expressions
        assert "if=user.amount > 0" in expressions, expressions
        assert "/if" in expressions, expressions
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("test_py3o_accepts_compiled_template OK")


def main():
    test_tokenizer_basic()
    test_block_rule_recognises_sole_directive()
    test_variable_substitution()
    test_forloop_emit()
    test_if_emit()
    test_function_directive()
    test_comments_are_dropped()
    test_lint_unbalanced_for()
    test_lint_refuses_elif()
    test_py3o_accepts_compiled_template()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
