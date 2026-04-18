"""Черновик рассылки → Telegram MarkdownV2 и HTML-предпросмотр для web-admin."""

from __future__ import annotations

import html as html_lib
import re

from shared.md2 import bold, code, italic, link, plain, pre, spoiler, strike, underline

_PH = "<<<PH_{}>>>"


def draft_to_markdown_v2(text: str) -> str:
    """
    Превращает черновик админки в валидный MarkdownV2 для Bot API.
    ```блок```, `инлайн`, [текст](url), ||спойлер||, **жирный**, *жирный*, __подчёркнутый__,
    ~~зачёркнутый~~, ~зачёркнутый~, _курсив_. Остальное — plain().
    """
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    vault: list[str] = []

    def stash(content: str) -> str:
        vault.append(content)
        return _PH.format(len(vault) - 1)

    s = raw

    s = re.sub(r"```([\s\S]*?)```", lambda m: stash(pre(m.group(1))), s)
    s = re.sub(r"`([^`\n]+)`", lambda m: stash(code(m.group(1))), s)
    s = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: stash(link(m.group(1).strip(), m.group(2).strip())),
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
        r"(?<![_])_([^_\n]+)_(?!_)",
        lambda m: stash(italic(m.group(1))),
        s,
    )

    parts: list[str] = []
    for chunk in re.split(r"(<<<PH_\d+>>>)", s):
        mm = re.fullmatch(r"<<<PH_(\d+)>>>", chunk)
        if mm:
            parts.append(vault[int(mm.group(1))])
        else:
            parts.append(plain(chunk))
    return "".join(parts)


def draft_to_preview_html(text: str) -> str:
    """
    HTML для пузырька предпросмотра; переносы строк — через класс whitespace-pre-wrap на контейнере.
    """
    raw = (text or "").replace("\r\n", "\n")
    if not raw.strip():
        return '<span class="opacity-70">Пусто</span>'

    def esc(s: str) -> str:
        return html_lib.escape(s)

    out = esc(raw)
    out = re.sub(r"```([\s\S]*?)```", lambda m: "<pre class=\"bc-prev-pre text-xs\">{}</pre>".format(esc(m.group(1))), out)
    out = re.sub(r"`([^`\n]+)`", lambda m: "<code class=\"bc-prev-code text-xs\">{}</code>".format(esc(m.group(1))), out)
    out = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: '<a class="underline text-sky-200 break-all" href="{}">{}</a>'.format(
            esc(m.group(2).strip()),
            esc(m.group(1).strip()),
        ),
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
    out = re.sub(r"(?<![_])_([^_\n]+)_(?!_)", lambda m: "<i>{}</i>".format(esc(m.group(1))), out)

    return f'<div class="bc-prev-bubble-inner whitespace-pre-wrap break-words text-left">{out}</div>'
