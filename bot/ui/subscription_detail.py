"""Текст экрана «Подписка» (детально), MarkdownV2 — лимиты из Remnawave."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError, subscription_url_for_telegram
from shared.integrations.rw_traffic import (
    extract_connected_devices_from_rw_user,
    extract_traffic_gb_from_rw_user,
    is_rw_traffic_unlimited,
    traffic_limit_gb_for_display,
)
from shared.md2 import bold, code, esc, italic, join_lines, plain
from shared.models.user import User
from shared.services.subscription_service import count_devices, get_active_subscription, get_base_subscription_plan

logger = logging.getLogger(__name__)

_MSK_TZ = ZoneInfo("Europe/Moscow")


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
    is_bot_admin: bool = False,
) -> tuple[str, str | None]:
    """
    Возвращает (подпись MarkdownV2, url подписки или None).
    Трафик и устройства: из панели Remnawave при наличии uuid; ∞ если лимит отключён в панели.
    Слоты: занято (HWID/панель) / куплено слотов в боте (sub.devices_count); у админа бота знаменатель ∞.
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

    uinf: dict | None = None
    sub_url: str | None = None
    hwid_list_ok = False
    hwid_devices_count = 0

    if user.remnawave_uuid:
        rw = RemnaWaveClient(settings)
        try:
            uinf = await rw.get_user(str(user.remnawave_uuid))
            sub_url = subscription_url_for_telegram(uinf.get("subscriptionUrl"), settings)
        except RemnaWaveError:
            logger.warning("RW get_user failed for subscription screen user=%s", user.id)
            uinf = None
        try:
            devs = await rw.get_user_hwid_devices(str(user.remnawave_uuid))
            hwid_list_ok = True
            hwid_devices_count = len(devs)
        except RemnaWaveError:
            logger.debug("RW get_user_hwid_devices failed user=%s", user.id)

    # --- Трафик: исп / макс (ГБ), макс из trafficLimitBytes; 0 = ∞
    if uinf:
        used_gb, _lim_unused = extract_traffic_gb_from_rw_user(uinf)
        used_part = bold(f"{used_gb:.2f}") if used_gb is not None else plain("—")
        if is_rw_traffic_unlimited(uinf):
            max_part = bold("∞")
        else:
            lim_gb = traffic_limit_gb_for_display(uinf)
            max_part = bold(f"{lim_gb:.1f}") if lim_gb is not None else plain("—")
        traffic_line = plain("📊 Трафик: ") + used_part + plain(" / ") + max_part + plain(" ГБ")
    else:
        if user.remnawave_uuid is None:
            traffic_line = plain("📊 Трафик: ") + italic("(нет учётной записи VPN)")
        else:
            limit_hint = (
                bold(f"{float(plan.traffic_limit_gb):.0f}") + plain(" ГБ")
                if plan and plan.traffic_limit_gb is not None and plan.traffic_limit_gb > 0
                else italic("без лимита в тарифе")
            )
            traffic_line = (
                plain("📊 Трафик: ")
                + italic("(данные панели недоступны)")
                + plain(" · лимит по тарифу в боте: ")
                + limit_hint
            )

    # --- Слоты: занято (список HWID или запасной счётчик) / куплено в боте (devices_count); админ бота → ∞
    if uinf:
        if hwid_list_ok:
            n_occupied = hwid_devices_count
        else:
            n_occupied = extract_connected_devices_from_rw_user(uinf)
            if n_occupied is None:
                n_occupied = await count_devices(session, sub.id)
    else:
        n_occupied = await count_devices(session, sub.id)

    denom_slots = bold("∞") if is_bot_admin else bold(str(sub.devices_count))
    devices_slots_line = (
        plain("📟 Слоты: ") + bold(str(n_occupied)) + plain(" / ") + denom_slots
    )

    exp = sub.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    left_phrase = _humanize_left(exp, now)
    exp_msk = exp.astimezone(_MSK_TZ).strftime("%d.%m.%Y %H:%M") + " МСК"

    header = "🔑 " + bold("Подписка:")
    quote_lines = [status_human]
    quote_lines.extend(
        [
        plain("💎 Тариф: ") + bold(plan.name if plan else "—"),
        traffic_line,
        devices_slots_line,
        plain("🗓️ До: ")
        + bold(exp_msk)
        + plain(" (")
        + esc(left_phrase)
        + plain(")"),
        ]
    )
    if sub.auto_renew and sub.status == "active":
        bp = await get_base_subscription_plan(session)
        if bp is not None and bp.price_rub > 0:
            quote_lines.append(
                plain("🔄 Авто-продление: ")
                + bold(str(int(bp.price_rub)) + " ₽")
                + plain(" за +")
                + bold(str(int(bp.duration_days)) + " дн.")
                + plain(" (~за 1 ч до окончания)")
            )
    quoted_block = "\n".join("> " + line for line in quote_lines)
    caption = join_lines(header, "", quoted_block)
    if sub_url:
        caption += "\n\n" + plain("📎 ") + bold("Ссылка:") + "\n" + code(sub_url)
    return caption, sub_url
