"""Однократная подготовка БД при старте бота или API с Telegram webhook."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.exc import OperationalError

from shared.database import get_session_factory
from shared.services.plan_seed import ensure_default_plans_if_needed
from shared.services.schema_patches import (
    ensure_promo_columns,
    ensure_subscription_expiry_notify_columns,
    ensure_user_bot_message_id_columns,
)

_DB_BOOTSTRAP_ATTEMPTS = 30
_DB_BOOTSTRAP_DELAY_SEC = 1.0


def _is_transient_db_connect_error(exc: BaseException) -> bool:
    cur: BaseException | None = exc
    for _ in range(12):
        if cur is None:
            return False
        if isinstance(cur, (OperationalError, OSError)):
            return True
        cur = cur.__cause__
    return False


async def bootstrap_bot_database_schema() -> None:
    """
    Планы по умолчанию и лёгкие schema patches.
    Повтор при временных сбоях DNS/Postgres (как при старте Docker).
    """
    log = logging.getLogger(__name__)
    factory = get_session_factory()
    last_err: BaseException | None = None
    for attempt in range(1, _DB_BOOTSTRAP_ATTEMPTS + 1):
        try:
            async with factory() as s:
                await ensure_default_plans_if_needed(s)
                await ensure_subscription_expiry_notify_columns(s)
                await ensure_promo_columns(s)
                await ensure_user_bot_message_id_columns(s)
                await s.commit()
            if attempt > 1:
                log.info("Подключение к БД восстановлено с попытки %s", attempt)
            return
        except Exception as e:
            if not _is_transient_db_connect_error(e):
                raise
            last_err = e
            log.warning(
                "БД недоступна при старте (попытка %s/%s): %s",
                attempt,
                _DB_BOOTSTRAP_ATTEMPTS,
                e,
            )
            if attempt >= _DB_BOOTSTRAP_ATTEMPTS:
                break
            await asyncio.sleep(_DB_BOOTSTRAP_DELAY_SEC)
    assert last_err is not None
    raise last_err
