# Copyright 2026 Open Eye Development
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Token grammar + tokenizer for the py3o preprocessor.

Grammar (Jinja2-style split syntax):

    {{ <expr> }}                 variable substitution; emits a py3o user-field
                                 with name ``py3o.<expr>`` (no surrounding
                                 whitespace in the stored name).

    {% for <name> in <expr> %}   opens a for-loop; closing tag ``{% endfor %}``.
    {% if <expr> %}              opens a conditional; closes with
                                 ``{% endif %}``. py3o does **not** support
                                 ``{% elif %}`` or ``{% else %}`` — use two
                                 adjacent ``{% if %}{% endif %}`` blocks
                                 (``{% if A %}…{% endif %}{% if not A %}…{% endif %}``).
    {% function="<call>" %}      py3o format-function invocation, e.g.
                                 ``{% function="format_currency(amount, 'EUR')" %}``.

    {# <anything> #}             comment; stripped from output entirely.

Expressions and directives **do not** execute at preprocessor time. The
preprocessor only validates their shape; py3o + Odoo evaluate them at render
time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

TokenKind = Literal["var", "directive", "comment"]

# Capture three marker kinds in a single scan. ``re.DOTALL`` lets multi-line
# block directives match. Non-greedy quantifiers stop at the first ``}}``/``%}``/
# ``#}}`` so nested markers don't confuse the tokenizer.
# NOTE: written on one line WITHOUT re.VERBOSE because ``#`` is a meta-char in
# VERBOSE mode and would swallow the ``{# ... #}`` alternative.
_TOKEN_RE = re.compile(
    r"""\{\{\s*(?P<var>.*?)\s*\}\}|\{%\s*(?P<directive>.*?)\s*%\}|\{#\s*(?P<comment>.*?)\s*#\}""",
    re.DOTALL,
)

# Direction grammars — used to validate the directive payload and split it into
# (instruction, value). Plain keywords that take no ``=`` payload are
# enumerated here so we don't confuse them with function calls.
_OPEN_DIRECTIVES = {"for", "if"}
_CLOSE_DIRECTIVES = {"endfor", "endif"}
_FUNCTION_RE = re.compile(
    r'^function\s*=\s*(["\'])(?P<value>.*)\1$',
    re.DOTALL,
)
# NOTE: py3o.template understands only ``for=``/``/for``, ``if=``/``/if`` and
# ``function=``. ``elif``/``else`` are *not* in the underlying engine; they
# get parsed as bare-variable-access and silently break the generated Python
# AST. The preprocessor refuses them with a clear pointer to the workaround.
_FOR_RE = re.compile(r"^for\s+(?P<target>[A-Za-z_][A-Za-z0-9_]*)\s+in\s+(?P<iter>.+)$")
_IF_RE = re.compile(r"^if\s+(?P<cond>.+)$")


@dataclass(frozen=True)
class Token:
    """A parsed Jinja2-style token from the source template."""

    kind: TokenKind
    raw: str
    # For ``var`` tokens: the stripped expression (without braces).
    # For ``directive`` tokens: the directive body ("for x in xs", "/for", ...).
    # For ``comment`` tokens: the comment text (kept for diagnostic only).
    body: str
    start: int
    end: int

    @property
    def is_open_directive(self) -> bool:
        return self.kind == "directive" and _split_instruction(self.body)[0] in (
            _OPEN_DIRECTIVES | {"function"}
        )

    @property
    def is_close_directive(self) -> bool:
        return self.kind == "directive" and _split_instruction(self.body)[0] in _CLOSE_DIRECTIVES


def _split_instruction(body: str) -> tuple[str, str]:
    """Return ``(instruction, value)`` from a directive body.

    ``instruction`` is the leading keyword (``for``/``if``/``elif``/``else``/
    ``endif``/``endfor``/``function``); ``value`` is whatever follows the first
    ``=`` for ``function`` directives, or the body remainder for control
    directives.
    """
    body = body.strip()
    m = _FUNCTION_RE.match(body)
    if m:
        return "function", m.group("value")
    head, _, rest = body.partition(" ")
    return head, rest.strip()


def tokenize(text: str) -> list[Token]:
    """Scan ``text`` and return the list of top-level tokens in order.

    Whitespace-only or token-free strings produce an empty list, so callers can
    check token counts cheaply to spot "lone directive paragraph".
    """
    tokens: list[Token] = []
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        if m.group("var") is not None:
            body = m.group("var")
            kind: TokenKind = "var"
        elif m.group("directive") is not None:
            body = m.group("directive")
            kind = "directive"
        else:
            body = m.group("comment")
            kind = "comment"
        tokens.append(Token(kind=kind, raw=m.group(0), body=body, start=m.start(), end=m.end()))
        pos = m.end()
    return tokens


def paragraph_is_sole_directive(text: str) -> Token | None:
    """If ``text`` is a paragraph whose stripped content is exactly one
    directive token surrounded only by whitespace, return that token.

    Used to decide whether a paragraph should be replaced body-and-all by a
    py3o hyperlink element (Liquid-style block-rule).
    """
    stripped = text.strip()
    if not (stripped.startswith("{%") and stripped.endswith("%}")):
        return None
    tokens = tokenize(stripped)
    if len(tokens) != 1:
        return None
    tok = tokens[0]
    if tok.kind != "directive":
        return None
    # Surrounding whitespace already stripped; the single token must consume
    # the whole paragraph.
    if tok.raw != stripped:
        return None
    return tok


def directive_parts(body: str) -> tuple[str, str, dict[str, str]]:
    """Split a directive body into ``(instruction, raw_value, parsed)``.

    ``parsed`` is a dict whose keys depend on the instruction:
    - ``for``: ``{"target": str, "iter": str}``
    - ``if`` / ``elif``: ``{"cond": str}``
    - ``function``: ``{"value": str}``
    - ``else``/``endif``/``endfor``: ``{}``

    Raises :class:`ValueError` for malformed directives so the caller can wrap
    the message in a :class:`PreprocessError` with file/position context.
    """
    instr, rest = _split_instruction(body)
    parsed: dict[str, str] = {}
    if instr in {"else", "elif"}:
        raise ValueError(
            f"py3o.template does not support '{instr}'. Rewrite using two "
            f"adjacent {{% if %}}...{{% endif %}} blocks (e.g. "
            f"{{% if A %}}...{{% endif %}}{{% if not A %}}...{{% endif %}}). "
            f"Got: {body!r}"
        )
    if instr == "for":
        m = _FOR_RE.match(body)
        if not m:
            raise ValueError(f"Malformed for directive: {body!r}")
        parsed = {"target": m.group("target"), "iter": m.group("iter").strip()}
    elif instr == "if":
        m = _IF_RE.match(body)
        if not m:
            raise ValueError(f"Malformed if directive: {body!r}")
        parsed = {"cond": m.group("cond").strip()}
    elif instr == "function":
        m = _FUNCTION_RE.match(body)
        if not m:
            raise ValueError(f"Malformed function directive: {body!r}")
        parsed = {"value": m.group("value")}
    elif instr in {"endfor", "endif"}:
        if rest:
            raise ValueError(
                f"Directive '{instr}' does not take a payload, got: {rest!r}"
            )
    else:
        raise ValueError(
            f"Unknown directive: {body!r}. Supported: "
            f"for/if/endif/endfor/function; comment via {{# ... #}}."
        )
    return instr, body.split("=", 1)[1].strip() if "=" in body and instr == "function" else rest, parsed
