"""Маркетинговые предложения на покупку тарифа.

1) Акция «2 недели за 1 ₽» (intro): разовая, только пока пользователь ни разу не покупал подписку.
   Отдельного тарифа в магазине нет — используется скрытый план INTRO_PLAN_NAME (is_active=False),
   который принимает только purchase_plan_with_balance и только при выполнении условий.

2) Скидка «вернись» (win-back), по умолчанию 20% на одну покупку. Защита от злоупотреблений:
   - только тем, кто уже платил за подписку (не триал/промо);
   - подписка закончилась не раньше чем WINBACK_MIN_DAYS назад и не позже WINBACK_MAX_DAYS
     (нельзя «дать подписке истечь на минуту» ради скидки — нужно реально пробыть без VPN);
   - предложение живёт WINBACK_OFFER_DAYS дней и действует на одну покупку;
   - повторно — не чаще раза в WINBACK_COOLDOWN_DAYS (по умолчанию полгода), даже если не воспользовался;
   - не суммируется с промокодом (берётся бо́льшая скидка) и не действует на акцию intro.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.database import get_session_factory
from shared.models.plan import Plan
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User

logger = logging.getLogger(__name__)

INTRO_PLAN_NAME = "Акция: 2 недели"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------------
# Акция intro
# --------------------------------------------------------------------------------------------


async def get_intro_plan(session: AsyncSession, settings: Settings) -> Plan:
    """Скрытый план акции (создаётся при первом обращении, в магазине не виден)."""
    plan = (await session.execute(select(Plan).where(Plan.name == INTRO_PLAN_NAME).limit(1))).scalar_one_or_none()
    days = int(settings.intro_offer_days)
    old_price = Decimal(settings.intro_offer_old_price_rub)
    if plan is None:
        plan = Plan(
            name=INTRO_PLAN_NAME,
            duration_days=days,
            price_rub=old_price,
            discount_percent=Decimal("0"),
            traffic_limit_gb=None,
            is_active=False,
            sort_order=999,
        )
        session.add(plan)
        await session.flush()
    elif plan.duration_days != days or plan.price_rub != old_price or plan.is_active:
        plan.duration_days = days
        plan.price_rub = old_price
        plan.is_active = False
    return plan


async def has_paid_subscription_purchase(session: AsyncSession, user_id: int) -> bool:
    row = (
        await session.execute(
            select(Transaction.id)
            .where(
                Transaction.user_id == user_id,
                Transaction.type == "subscription",
                Transaction.status == "completed",
                Transaction.amount > 0,
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def intro_offer_eligible(session: AsyncSession, user: User, settings: Settings) -> bool:
    if not settings.intro_offer_enabled or user.intro_offer_used_at is not None or user.is_blocked:
        return False
    return not await has_paid_subscription_purchase(session, user.id)


def intro_offer_texts(settings: Settings) -> dict:
    days = int(settings.intro_offer_days)
    weeks = days // 7 if days % 7 == 0 else None
    period = (f"{weeks} недели" if weeks in (2, 3, 4) else f"{weeks} неделю" if weeks == 1 else f"{days} дн.")
    return {
        "period": period,
        "price": Decimal(settings.intro_offer_price_rub),
        "old_price": Decimal(settings.intro_offer_old_price_rub),
        "title": f"{period} за {Decimal(settings.intro_offer_price_rub).normalize():f} ₽",
    }


async def intro_offer_button_text(session: AsyncSession, user: User | None, settings: Settings) -> str | None:
    """Текст кнопки акции для главного меню бота или None, если акция пользователю недоступна."""
    if user is None or not await intro_offer_eligible(session, user, settings):
        return None
    t = intro_offer_texts(settings)
    return f"🔥 {t['title']} вместо {t['old_price'].normalize():f} ₽"


# --------------------------------------------------------------------------------------------
# Win-back
# --------------------------------------------------------------------------------------------


def winback_active(user: User, now: datetime | None = None) -> bool:
    now = now or _now()
    until = _utc(user.winback_offer_until)
    if until is None or until <= now:
        return False
    used = _utc(user.winback_offer_used_at)
    offered = _utc(user.winback_offered_at)
    return used is None or (offered is not None and used < offered)


def winback_percent(user: User, settings: Settings) -> Decimal:
    if not settings.winback_enabled or not winback_active(user):
        return Decimal("0")
    return Decimal(settings.winback_discount_percent)


@dataclass
class PriceQuote:
    """Цена тарифа для конкретного пользователя с учётом всех предложений (для витрин бота/сайта/mini app)."""

    original: Decimal  # без скидки (база × месяцы или «старая» цена акции)
    final: Decimal  # к списанию
    discount_percent: Decimal  # скидка промокода/win-back поверх цены тарифа
    source: str  # "" | "promo" | "winback" | "intro"
    promo_code: str | None = None


def apply_percent(price: Decimal, pct: Decimal) -> Decimal:
    """Та же арифметика, что при списании в purchase_plan_with_balance (до копеек)."""
    if pct <= 0:
        return price
    return (price - (price * pct / Decimal("100")).quantize(Decimal("0.01"))).quantize(Decimal("0.01"))


async def quote_plan_price(session: AsyncSession, user: User, plan: Plan, settings: Settings) -> PriceQuote:
    from shared.services.promo_service import get_pending_purchase_discount_info
    from shared.services.subscription_service import (
        get_one_month_reference_plan,
        resolve_user_plan_price_rub,
        tariff_duration_months,
        user_custom_month_price_rub,
    )

    if plan.name == INTRO_PLAN_NAME:
        t = intro_offer_texts(settings)
        return PriceQuote(original=t["old_price"], final=t["price"], discount_percent=Decimal("0"), source="intro")
    base = await resolve_user_plan_price_rub(session, user, plan)
    promo_code, promo_pct = await get_pending_purchase_discount_info(session, user_id=user.id)
    wb = winback_percent(user, settings)
    pct, source = Decimal("0"), ""
    if wb > 0 and wb >= promo_pct:
        pct, source, promo_code = wb, "winback", None
    elif promo_pct > 0:
        pct, source = promo_pct, "promo"
    final = apply_percent(base, pct)
    ref = await get_one_month_reference_plan(session)
    custom = user_custom_month_price_rub(user)
    base_month = custom if custom is not None else (ref.price_rub if ref else Decimal("0"))
    months = tariff_duration_months(plan.duration_days)
    original = (base_month * months).quantize(Decimal("1")) if base_month > 0 else base
    return PriceQuote(original=max(original, final), final=final, discount_percent=pct, source=source, promo_code=promo_code)


async def process_winback_offers(session: AsyncSession, settings: Settings) -> list[User]:
    """Выдаёт предложение подходящим пользователям. Возвращает тех, кому выдали (для уведомлений)."""
    if not settings.winback_enabled:
        return []
    now = _now()
    min_gap = now - timedelta(days=int(settings.winback_min_days_after_expiry))
    max_gap = now - timedelta(days=int(settings.winback_max_days_after_expiry))
    cooldown = now - timedelta(days=int(settings.winback_cooldown_days))

    paid = exists().where(
        Transaction.user_id == User.id,
        Transaction.type == "subscription",
        Transaction.status == "completed",
        Transaction.amount > 0,
    )
    active = exists().where(
        Subscription.user_id == User.id,
        Subscription.status.in_(("active", "trial")),
        Subscription.expires_at > now,
    )
    last_exp = (
        select(func.max(Subscription.expires_at)).where(Subscription.user_id == User.id).correlate(User).scalar_subquery()
    )
    q = (
        select(User)
        .where(
            User.is_blocked.is_(False),
            paid,
            ~active,
            last_exp <= min_gap,
            last_exp >= max_gap,
            (User.winback_offered_at.is_(None)) | (User.winback_offered_at < cooldown),
        )
        .limit(200)
    )
    users = list((await session.execute(q)).scalars().all())
    until = now + timedelta(days=int(settings.winback_offer_days))
    for u in users:
        u.winback_offered_at = now
        u.winback_offer_until = until
        u.winback_offer_used_at = None
    return users


async def _notify_winback(user: User, settings: Settings) -> None:
    from shared.services.telegram_notify import send_telegram_message

    pct = Decimal(settings.winback_discount_percent).normalize()
    until = _utc(user.winback_offer_until)
    from shared.datetime_msk import fmt_dt_msk

    text = (
        f"💜 Мы скучаем! Для вас персональная скидка {pct:f}% на любой тариф.\n"
        f"Действует до {fmt_dt_msk(until)} на одну покупку — оформить можно в разделе «Подписка»."
    )
    kb = {"inline_keyboard": [[{"text": f"🎁 Продлить со скидкой {pct:f}%", "callback_data": "sub:plans"}]]}
    try:
        await send_telegram_message(user.telegram_id, text, parse_mode=None, settings=settings, reply_markup=kb)
    except Exception:
        logger.exception("winback: telegram notify failed user=%s", user.id)
    # На почту — только тем, кто согласился на рекламную рассылку.
    if user.email and user.email_verified_at and user.email_marketing_consent:
        from shared.services.email_marketing import unsubscribe_url
        from shared.services.email_sender import email_sending_configured, send_branded_email, site_url

        if email_sending_configured(settings):
            await send_branded_email(
                user.email,
                subject=f"🎁 Скидка {pct:f}% на возвращение в Flux VPN",
                title=f"Вернитесь со скидкой {pct:f}%",
                intro="Мы заметили, что ваша подписка закончилась. Приготовили персональную скидку на любой тариф — "
                "подключение сохранится, ничего настраивать заново не нужно.",
                button_text="Выбрать тариф",
                button_url=site_url(settings, "/app/subscription") or None,
                note=f"Скидка действует до {fmt_dt_msk(until)} на одну покупку.",
                badge="Персональное предложение",
                unsubscribe_url=unsubscribe_url(user, settings),
                settings=settings,
            )


async def winback_offer_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    interval = 3600
    while not stop_event.is_set():
        try:
            if settings.winback_enabled:
                factory = get_session_factory()
                async with factory() as session:
                    async with session.begin():
                        users = await process_winback_offers(session, settings)
                for u in users:
                    await _notify_winback(u, settings)
                if users:
                    logger.info("winback: выдано предложений %s", len(users))
        except Exception:
            logger.exception("winback: итерация не удалась")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
