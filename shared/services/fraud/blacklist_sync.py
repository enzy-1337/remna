"""Синк внешнего списка нарушителей (Telegram ID) — см. Settings.fraud_blacklist_url.

Список трактуется как append-only: удаление ID из апстрима НЕ приводит к авторазблокировке
(снятие блока по внешнему сигналу рискованнее, чем оставить как есть — это решение админа).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.telegram_blacklist_entry import TelegramBlacklistEntry
from shared.models.telegram_blacklist_sync_state import TelegramBlacklistSyncState
from shared.models.user import User
from shared.services.fraud.blacklist_action import apply_blacklist_block

logger = logging.getLogger(__name__)

SOURCE_EXTERNAL = "bedolaga_dev"


def parse_telegram_id_list(text: str) -> set[int]:
    out: set[int] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.add(int(line))
        except ValueError:
            continue
    return out


async def sync_telegram_blacklist(session: AsyncSession, settings: Settings) -> int:
    """Тянет актуальный список, добавляет новые записи, немедленно блокирует уже
    зарегистрированных пользователей среди них. Возвращает число вновь заблокированных.
    """
    state = await session.get(TelegramBlacklistSyncState, 1)
    if state is None:
        state = TelegramBlacklistSyncState(id=1)
        session.add(state)
        await session.flush()

    headers = {"If-None-Match": state.etag} if state.etag else {}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(settings.fraud_blacklist_url, headers=headers)
    except Exception:
        logger.exception("fraud blacklist sync: fetch failed")
        return 0

    if resp.status_code == 304:
        state.last_synced_at = datetime.now(timezone.utc)
        return 0
    if resp.status_code != 200:
        logger.warning("fraud blacklist sync: unexpected status %s", resp.status_code)
        return 0

    new_ids = parse_telegram_id_list(resp.text)
    etag = resp.headers.get("ETag")

    existing_ids = set(
        (await session.execute(select(TelegramBlacklistEntry.telegram_id))).scalars().all()
    )
    added_ids = new_ids - existing_ids
    for tg_id in added_ids:
        session.add(TelegramBlacklistEntry(telegram_id=tg_id, source=SOURCE_EXTERNAL))
    if added_ids:
        await session.flush()

    blocked_count = 0
    if added_ids:
        rows = (
            await session.execute(
                select(User).where(User.telegram_id.in_(added_ids), User.is_blocked.is_(False))
            )
        ).scalars().all()
        for u in rows:
            await apply_blacklist_block(session, settings, u, source=SOURCE_EXTERNAL)
            blocked_count += 1

    state.etag = etag
    state.last_synced_at = datetime.now(timezone.utc)
    state.last_id_count = len(new_ids)
    return blocked_count
