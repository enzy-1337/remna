"""Черновик рассылки → Telegram MarkdownV2 и HTML-предпросмотр для web-admin."""

from __future__ import annotations

import html as html_lib
import re

from shared.md2 import bold, code, italic, link, plain, pre, spoiler, strike, underline

# Без «_» перед цифрой: иначе шаблон _0_ в «PH_0» срабатывает как _курсив_ в regex.
_PH = "<<<PH{}>>>"


def draft_to_markdown_v2(text: str) -> str:
    """
    Превращает черновик админки в валидный MarkdownV2 для Bot API.
    ```блок```, `инлайн`, [текст](url), ||спойлер||, **жирный**, *жирный*, __подчёркнутый__,
    ~~зачёркнутый~~, ~зачёркнутый~, _курсив_. Строки с префиксом «>» — цитата (рекурсивно).
    Остальное — plain().
    """
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    vault: list[str] = []

    def stash(content: str) -> str:
        vault.append(content)
        return _PH.format(len(vault) - 1)

    def stash_blockquotes(s: str) -> str:
        lines = s.split("\n")
        out_lines: list[str] = []
        i = 0
        while i < len(lines):
            if re.match(r"^\s*>", lines[i]):
                inner_lines: list[str] = []
                while i < len(lines) and re.match(r"^\s*>", lines[i]):
                    inner_lines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                    i += 1
                inner_raw = "\n".join(inner_lines)
                md_inner = draft_to_markdown_v2(inner_raw)
                prefixed = (
                    "\n".join((">" + line if line else ">") for line in md_inner.split("\n"))
                    if md_inner
                    else ">"
                )
                out_lines.append(stash(prefixed))
            else:
                out_lines.append(lines[i])
                i += 1
        return "\n".join(out_lines)

    s = stash_blockquotes(raw)

    s = re.sub(r"```([\s\S]*?)```", lambda m: stash(pre(m.group(1))), s)
    s = re.sub(r"`([^`\n]+)`", lambda m: stash(code(m.group(1))), s)
    s = re.sub(
        r"\[([^\]]+)\]\s*\(([^)]+)\)",
        lambda m: stash(link(m.group(1).strip(), m.group(2).strip())),
        s,
    )
    # @username -> явная ссылка на t.me; делаем до курсива/подчёркивания, чтобы "_" не ломали ник
    s = re.sub(
        r"(?<![\w/])@([A-Za-z0-9_]{5,32})",
        lambda m: stash(link("@" + m.group(1), f"https://t.me/{m.group(1)}")),
        s,
    )
    s = re.sub(r"\|\|(.+?)\|\|", lambda m: stash(spoiler(m.group(1))), s, flags=re.DOTALL)
    s = re.sub(r"\*\*(.+?)\*\*", lambda m: stash(bold(m.group(1))), s, flags=re.DOTALL)
    s = re.sub(
        r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)",
        lambda m: stash(bold(m.group(1))),
        s,
        flags=re.DOTALL,
    )
    s = re.sub(r"__(.+?)__", lambda m: stash(underline(m.group(1))), s, flags=re.DOTALL)
    s = re.sub(r"~~(.+?)~~", lambda m: stash(strike(m.group(1))), s, flags=re.DOTALL)
    s = re.sub(r"(?<![~])~([^~\n]+)~(?![~])", lambda m: stash(strike(m.group(1))), s)
    s = re.sub(
        r"(?<!\\)(?<![_])_([^_\n]+)(?<!\\)_(?!_)",
        lambda m: stash(italic(m.group(1))),
        s,
    )

    parts: list[str] = []
    for chunk in re.split(r"(<<<PH\d+>>>)", s):
        mm = re.fullmatch(r"<<<PH(\d+)>>>", chunk)
        if mm:
            parts.append(vault[int(mm.group(1))])
        else:
            parts.append(plain(chunk))
    return "".join(parts)


def _preview_format_chunk(chunk: str) -> str:
    """Разметка превью для одного фрагмента без stash-плейсхолдеров."""

    def esc(s: str) -> str:
        return html_lib.escape(s)

    out = esc(chunk)
    out = re.sub(
        r"```([\s\S]*?)```",
        lambda m: "<pre class=\"bc-prev-pre text-xs\">{}</pre>".format(esc(m.group(1))),
        out,
    )
    out = re.sub(
        r"`([^`\n]+)`",
        lambda m: "<code class=\"bc-prev-code text-xs\">{}</code>".format(esc(m.group(1))),
        out,
    )
    out = re.sub(
        r"\[([^\]]+)\]\s*\(([^)]+)\)",
        lambda m: '<a class="underline text-sky-200 break-all" href="{}">{}</a>'.format(
            esc(m.group(2).strip()),
            esc(m.group(1).strip()),
        ),
        out,
    )
    out = re.sub(
        r"(?<![\w/])@([A-Za-z0-9_]{5,32})",
        lambda m: '<a class="underline text-sky-200" href="https://t.me/{u}">@{u}</a>'.format(u=esc(m.group(1))),
        out,
    )
    out = re.sub(
        r"\|\|(.+?)\|\|",
        lambda m: '<span class="bg-white/15 rounded px-0.5">{}</span>'.format(esc(m.group(1))),
        out,
        flags=re.DOTALL,
    )
    out = re.sub(r"\*\*(.+?)\*\*", lambda m: "<b>{}</b>".format(esc(m.group(1))), out, flags=re.DOTALL)
    out = re.sub(
        r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)",
        lambda m: "<b>{}</b>".format(esc(m.group(1))),
        out,
        flags=re.DOTALL,
    )
    out = re.sub(r"__(.+?)__", lambda m: "<u>{}</u>".format(esc(m.group(1))), out, flags=re.DOTALL)
    out = re.sub(r"~~(.+?)~~", lambda m: "<s>{}</s>".format(esc(m.group(1))), out, flags=re.DOTALL)
    out = re.sub(r"(?<![~])~([^~\n]+)~(?![~])", lambda m: "<s>{}</s>".format(esc(m.group(1))), out)
    out = re.sub(
        r"(?<!\\)(?<![_])_([^_\n]+)(?<!\\)_(?!_)",
        lambda m: "<i>{}</i>".format(esc(m.group(1))),
        out,
    )
    return out


def draft_to_preview_html(text: str, *, wrap: bool = True) -> str:
    """
    HTML для пузырька предпросмотра; переносы строк — через класс whitespace-pre-wrap на контейнере.
    """
    raw = (text or "").replace("\r\n", "\n")
    if not raw.strip():
        return '<span class="opacity-70">Пусто</span>' if wrap else ""

    vault: list[str] = []

    def stash(html: str) -> str:
        vault.append(html)
        return _PH.format(len(vault) - 1)

    def stash_blockquotes_preview(s: str) -> str:
        lines = s.split("\n")
        out_lines: list[str] = []
        i = 0
        while i < len(lines):
            if re.match(r"^\s*>", lines[i]):
                inner_lines: list[str] = []
                while i < len(lines) and re.match(r"^\s*>", lines[i]):
                    inner_lines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                    i += 1
                inner_raw = "\n".join(inner_lines)
                inner_html = draft_to_preview_html(inner_raw, wrap=False)
                out_lines.append(
                    stash(
                        '<blockquote class="border-l-4 border-white/40 pl-2 my-1 opacity-95">'
                        f"{inner_html}</blockquote>"
                    )
                )
            else:
                out_lines.append(lines[i])
                i += 1
        return "\n".join(out_lines)

    s = stash_blockquotes_preview(raw)

    parts: list[str] = []
    for chunk in re.split(r"(<<<PH\d+>>>)", s):
        mm = re.fullmatch(r"<<<PH(\d+)>>>", chunk)
        if mm:
            parts.append(vault[int(mm.group(1))])
        else:
            parts.append(_preview_format_chunk(chunk))

    inner_result = "".join(parts)
    if wrap:
        return (
            f'<div class="bc-prev-bubble-inner whitespace-pre-wrap break-words text-left">{inner_result}</div>'
        )
    return inner_result
