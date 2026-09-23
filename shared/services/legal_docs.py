"""Юридические документы на своём сайте (/legal/privacy, /legal/terms) вместо Telegra.ph.

Текст хранится в таблице site_documents. Если документа ещё нет — при первом открытии он
автоматически импортируется из Telegra.ph (LEGAL_*_SOURCE_URL) через официальный API getPage.
Дальше его можно править или переимпортировать в web-admin: Настройки → «Документы сайта».
HTML проходит белый список тегов (без скриптов/стилей/iframe).
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

# slug → (заголовок по умолчанию, атрибут настроек с источником в Telegra.ph)
DOCS: dict[str, tuple[str, str]] = {
    "privacy": ("Политика конфиденциальности", "legal_privacy_source_url"),
    "terms": ("Пользовательское соглашение", "legal_terms_source_url"),
}

_ALLOWED = {"p", "br", "b", "strong", "i", "em", "u", "s", "a", "ul", "ol", "li", "blockquote", "h3", "h4", "hr", "aside", "code"}
_VOID = {"br", "hr"}

_DDL = """
CREATE TABLE IF NOT EXISTS site_documents (
    slug VARCHAR(32) PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    content_html TEXT NOT NULL,
    source_url VARCHAR(500) NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
)
"""


def public_doc_url(settings: Settings, slug: str) -> str:
    """Ссылка на документ на нашем сайте (для бота, писем и т.п.)."""
    base = (settings.public_site_url or "").strip().rstrip("/")
    return f"{base}/legal/{slug}" if base else ""


# --------------------------------------------------------------------------------------------
# Санитайзер
# --------------------------------------------------------------------------------------------


def _safe_href(url: str) -> str | None:
    u = (url or "").strip()
    if u.startswith(("https://", "http://", "mailto:", "tg://", "/")) and not u.startswith("//"):
        return u
    return None


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        tag = tag.lower()
        if tag not in _ALLOWED:
            return
        if tag == "a":
            href = _safe_href(dict(attrs).get("href") or "")
            if href is None:
                return
            self.out.append(f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener nofollow">')
        else:
            self.out.append(f"<{tag}>")
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):  # noqa: ANN001
        tag = tag.lower()
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):  # noqa: ANN001
        self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_html(raw: str) -> str:
    p = _Sanitizer()
    p.feed(raw or "")
    p.close()
    return p.result()


def plain_text_to_html(raw: str) -> str:
    """Если в редакторе вставили обычный текст — абзацы по пустым строкам, переносы — <br>."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", (raw or "").replace("\r\n", "\n")) if b.strip()]
    return "".join("<p>" + html.escape(b).replace("\n", "<br>") + "</p>" for b in blocks)


# --------------------------------------------------------------------------------------------
# Telegra.ph
# --------------------------------------------------------------------------------------------


def telegraph_path(url: str) -> str | None:
    u = urlparse((url or "").strip())
    if u.hostname not in ("telegra.ph", "graph.org", "te.legra.ph"):
        return None
    path = u.path.strip("/")
    return path or None


def _nodes_to_html(nodes) -> str:  # noqa: ANN001
    out: list[str] = []
    for n in nodes or []:
        if isinstance(n, str):
            out.append(html.escape(n, quote=False))
            continue
        if not isinstance(n, dict):
            continue
        tag = str(n.get("tag") or "").lower()
        inner = _nodes_to_html(n.get("children"))
        if tag not in _ALLOWED:
            out.append(inner)  # неподдерживаемый тег — оставляем только текст
            continue
        if tag in _VOID:
            out.append(f"<{tag}>")
        elif tag == "a":
            href = _safe_href((n.get("attrs") or {}).get("href") or "")
            out.append(f'<a href="{html.escape(href, quote=True)}">{inner}</a>' if href else inner)
        else:
            out.append(f"<{tag}>{inner}</{tag}>")
    return "".join(out)


async def fetch_from_telegraph(url: str) -> tuple[str, str]:
    """(title, html) страницы Telegra.ph через api.telegra.ph/getPage."""
    path = telegraph_path(url)
    if not path:
        raise ValueError("Это не ссылка на telegra.ph")
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(f"https://api.telegra.ph/getPage/{path}", params={"return_content": "true"})
        r.raise_for_status()
        data = r.json()
    if not data.get("ok"):
        raise ValueError(f"Telegra.ph: {data.get('error') or 'страница не найдена'}")
    res = data.get("result") or {}
    return str(res.get("title") or ""), sanitize_html(_nodes_to_html(res.get("content")))


# --------------------------------------------------------------------------------------------
# Хранилище
# --------------------------------------------------------------------------------------------


async def ensure_table(session: AsyncSession) -> None:
    await session.execute(text(_DDL))


async def get_doc(session: AsyncSession, slug: str) -> dict | None:
    await ensure_table(session)
    row = (
        await session.execute(
            text("SELECT slug, title, content_html, source_url, updated_at FROM site_documents WHERE slug=:s"), {"s": slug}
        )
    ).mappings().first()
    return dict(row) if row else None


async def save_doc(session: AsyncSession, slug: str, *, title: str, content_html: str, source_url: str | None) -> None:
    await ensure_table(session)
    await session.execute(
        text(
            """
            INSERT INTO site_documents (slug, title, content_html, source_url, updated_at)
            VALUES (:s, :t, :c, :u, :now)
            ON CONFLICT (slug) DO UPDATE
               SET title = EXCLUDED.title, content_html = EXCLUDED.content_html,
                   source_url = EXCLUDED.source_url, updated_at = EXCLUDED.updated_at
            """
        ),
        {"s": slug, "t": title, "c": content_html, "u": source_url, "now": datetime.now(timezone.utc)},
    )


def fill_placeholders(body: str, settings: Settings) -> str:
    """Заполняет незаполненные поля шаблона из Telegra.ph: «Укажите актуальную дату» и «ваш сайт/ тг бот»."""
    from shared.datetime_msk import fmt_dt_msk

    today = fmt_dt_msk(datetime.now(timezone.utc), with_suffix=False).split(" ")[0]
    body = re.sub(r"[Уу]кажите актуальную дату", f"Редакция от {today}", body)
    site = urlparse((settings.public_site_url or "").strip()).hostname or ""
    bot = (settings.bot_username or "").strip().lstrip("@")
    parts = [p for p in (site, f"Telegram-бот @{bot}" if bot else "") if p]
    if parts:
        body = re.sub(r"ваш сайт\s*/\s*тг бот", html.escape(" и ".join(parts), quote=False), body, flags=re.IGNORECASE)
    return body


async def import_doc(session: AsyncSession, slug: str, source_url: str) -> dict:
    title, body = await fetch_from_telegraph(source_url)
    body = fill_placeholders(body, get_settings())
    default_title = DOCS.get(slug, ("Документ", ""))[0]
    await save_doc(session, slug, title=title or default_title, content_html=body, source_url=source_url)
    return {"title": title or default_title, "content_html": body, "source_url": source_url}


async def get_or_import_doc(session: AsyncSession, slug: str, settings: Settings | None = None) -> dict | None:
    """Документ из БД; если его ещё нет — однократный импорт из Telegra.ph по ссылке из настроек."""
    s = settings or get_settings()
    doc = await get_doc(session, slug)
    if doc is not None:
        return doc
    if slug not in DOCS:
        return None
    src = (getattr(s, DOCS[slug][1]) or "").strip()
    if not src:
        return None
    try:
        doc = await import_doc(session, slug, src)
        await session.commit()
        logger.info("legal_docs: импортирован %s из %s", slug, src)
        return doc
    except Exception:
        logger.exception("legal_docs: не удалось импортировать %s из %s", slug, src)
        return None
