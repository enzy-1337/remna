"""Текст экрана «Подписка» (детально), MarkdownV2."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.integrations.rw_traffic import extract_traffic_gb_from_rw_user
from shared.md2 import bold, code, esc, italic, join_lines, plain
from shared.models.user import User
from shared.services.subscription_service import (
    MAX_DEVICES,
    count_devices,
    get_active_subscription,
)

logger = logging.getLogger(__name__)


def _ru_days_phrase(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return f"{n} дня"
    return f"{n} дней"


def _humanize_left(exp: datetime, now: datetime) -> str:
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    left = exp - now
    if left.total_seconds() <= 0:
        return "истекла"
    d = left.days
    h = left.seconds // 3600
    if d >= 1:
        return _ru_days_phrase(d)
    if h >= 1:
        return f"{h} ч."
    m = left.seconds // 60
    if m >= 1:
        return f"{m} мин."
    return "меньше минуты"


def _monthly_price_line(plan) -> str:
    if not plan or plan.price_rub <= 0:
        return esc("Триал / без оплаты за период")
    d = max(1, int(plan.duration_days))
    monthly = (plan.price_rub * Decimal(30)) / Decimal(d)
    m = monthly.quantize(Decimal("0.01"))
    return esc(f"≈ {m} ₽/мес (разово {plan.price_rub} ₽ за {d} дн.)")


async def build_subscription_detail_caption(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
) -> tuple[str, str | None]:
    """
    Возвращает (подпись MarkdownV2, url подписки или None).
    """
    sub = await get_active_subscription(session, user.id)
    now = datetime.now(timezone.utc)
    if not sub:
        return (
            join_lines(
                "🔑 " + bold("Подписка"),
                "",
                plain("Нет активной подписки."),
                plain("Оформите тариф или активируйте триал."),
            ),
            None,
        )

    plan = sub.plan
    status_human = "🟢 Активна" if sub.status in ("active", "trial") else f"⚪ {esc(sub.status)}"

    used_gb: float | None = None
    limit_gb: float | None = None
    sub_url: str | None = None

    if user.remnawave_uuid:
        rw = RemnaWaveClient(settings)
        try:
            uinf = await rw.get_user(str(user.remnawave_uuid))
            used_gb, limit_gb = extract_traffic_gb_from_rw_user(uinf)
            sub_url = uinf.get("subscriptionUrl") or None
        except RemnaWaveError:
            logger.warning("RW get_user failed for subscription screen user=%s", user.id)

    if plan and plan.traffic_limit_gb is not None and plan.traffic_limit_gb > 0:
        limit_gb = float(plan.traffic_limit_gb)

    if used_gb is None:
        used_gb = 0.0
    if limit_gb is not None:
        traffic_line = (
            plain("📊 Трафик: ")
            + bold(f"{used_gb:.1f}")
            + plain("/")
            + bold(f"{limit_gb:.1f}")
            + plain(" ГБ")
        )
    else:
        traffic_line = (
            plain("📊 Трафик: ")
            + bold(f"{used_gb:.1f}")
            + plain(" ГБ ")
            + italic("(без лимита)")
        )

    n_dev = await count_devices(session, sub.id)
    exp = sub.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    left_phrase = _humanize_left(exp, now)

    # Текст подписки делаем блоком-цитатой (`>` для каждой строки).
    lines = [
        "🔑 " + bold("Подписка:"),
        status_human,
        plain("💎 Тариф: ") + bold(plan.name if plan else "—"),
        traffic_line,
        plain("📟 Лимит устройств: ")
        + bold(str(sub.devices_count))
        + plain("/")
        + bold(str(MAX_DEVICES)),
        plain("🔄 Привязанных устройств: ") + bold(str(n_dev)),
        plain("🗓️ До: ")
        + bold(exp.strftime("%d.%m.%Y %H:%M"))
        + plain(" (")
        + esc(left_phrase)
        + plain(")"),
        plain("💸 Стоимость: ") + _monthly_price_line(plan),
    ]
    caption = "\n".join(["> " + l for l in lines])
    if sub_url:
        caption += "\n\n" + plain("📎 ") + bold("Ссылка:") + "\n" + code(sub_url)
    return caption, sub_url
