"""Telegram MarkdownV2: экранирование и обёртки (жирный, курсив, подчёркнутый, …)."""

from __future__ import annotations

# Символы, которые нужно экранировать вне «сущностей» (см. документацию Bot API)
_MD2_SPECIAL = frozenset(r"_*[]()~`>#+-=|{}.!")


def esc(text: str) -> str:
    """Экранирование произвольного пользовательского текста для MarkdownV2."""
    return "".join("\\" + c if c in _MD2_SPECIAL else c for c in text)


def bold(inner: str) -> str:
    return f"*{esc(inner)}*"


def italic(inner: str) -> str:
    return f"_{esc(inner)}_"


def underline(inner: str) -> str:
    return f"__{esc(inner)}__"


def strike(inner: str) -> str:
    return f"~{esc(inner)}~"


def spoiler(inner: str) -> str:
    return f"||{esc(inner)}||"


def code(inner: str) -> str:
    inner_esc = inner.replace("\\", r"\\").replace("`", r"\`")
    return f"`{inner_esc}`"


def pre(inner: str) -> str:
    inner_esc = inner.replace("\\", r"\\").replace("`", r"\`")
    return f"```{inner_esc}```"


def quote_block(text: str) -> str:
    """Цитата: каждая строка с префиксом >."""
    lines = text.split("\n")
    return "\n".join(">" + esc(line) if line else ">" for line in lines)


def join_lines(*parts: str) -> str:
    return "\n".join(parts)


def link(text: str, url: str) -> str:
    """Инлайн-ссылка MarkdownV2: в URL экранируются \\ и )."""
    u = url.replace("\\", r"\\").replace(")", r"\)")
    return f"[{esc(text)}]({u})"
