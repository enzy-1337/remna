"""Транзакционные письма на привязанную почту: чек о пополнении, продление, «осталось 3 дня», «осталось 6 часов».

Отправляются всем с подтверждённой почтой — независимо от согласия на рекламную рассылку.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.datetime_msk import fmt_dt_msk
from shared.models.user import User
from shared.services.email_sender import (
    EmailRow,
    email_sending_configured,
    send_after_commit,
    send_branded_email,
    site_url,
)

logger = logging.getLogger(__name__)

# Окна «осталось до конца» с запасом под интервал опроса (по умолчанию 5 мин)
WINDOW_3D = (timedelta(days=2, hours=22), timedelta(days=3, hours=1))
WINDOW_6H = (timedelta(hours=5), timedelta(hours=6, minutes=30))


def user_email_for_notify(user: User | None, settings: Settings) -> str | None:
    """Почта, на которую можно слать уведомления (только подтверждённая)."""
    if user is None or user.is_blocked:
        return None
    if not settings.subscription_email_notify_enabled or not email_sending_configured(settings):
        return None
    email = (user.email or "").strip()
    if not email or user.email_verified_at is None:
        return None
    return email


def _user_display_name(user: User) -> str:
    name = (user.first_name or user.username or "").strip()
    return name or "друг"


def _left_human(remaining: timedelta) -> str:
    hours = int(remaining.total_seconds() // 3600)
    if hours >= 48:
        days = round(remaining.total_seconds() / 86400)
        return f"{days} дн."
    if hours >= 1:
        return f"{hours} ч."
    return f"{max(1, int(remaining.total_seconds() // 60))} мин."


def queue_subscription_renewed_email(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
    new_expires: datetime,
    plan_name: str,
    price_rub: Decimal | None = None,
    auto: bool = False,
) -> None:
    """Ставит письмо «подписка продлена» в очередь — уйдёт после коммита транзакции."""
    email = user_email_for_notify(user, settings)
    if email is None:
        return
    name = _user_display_name(user)
    rows = [
        EmailRow("Тариф", plan_name),
        EmailRow("Действует до", fmt_dt_msk(new_expires)),
    ]
    if price_rub is not None and price_rub > 0:
        rows.append(EmailRow("Списано с баланса", f"{price_rub} ₽"))
    if auto:
        rows.append(EmailRow("Способ", "Автопродление"))
    title = "Подписка продлена" if not auto else "Подписка автоматически продлена"

    async def _send() -> None:
        ok, err = await send_branded_email(
            email,
            subject=f"✅ {title} до {fmt_dt_msk(new_expires, with_suffix=False)}",
            title=title,
            intro=f"{name}, спасибо, что остаётесь с нами! Доступ к VPN продлён — ничего настраивать заново не нужно.",
            rows=rows,
            button_text="Открыть личный кабинет",
            button_url=site_url(settings, "/app/subscription") or None,
            note="Если продление сделали не вы — напишите в поддержку.",
            tone="success",
            badge="Оплата прошла",
            settings=settings,
        )
        if not ok:
            logger.warning("renew email failed user=%s: %s", user.id, err)

    send_after_commit(session, _send)


async def send_expiry_email(
    *,
    user: User,
    settings: Settings,
    expires_at: datetime,
    kind: str,
    is_trial: bool = False,
) -> bool:
    """kind: '3d' | '6h'. Возвращает True, если письмо отправлено."""
    email = user_email_for_notify(user, settings)
    if email is None:
        return False
    now = datetime.now(timezone.utc)
    remaining = expires_at - now
    what = "Пробный период" if is_trial else "Подписка"
    left = _left_human(remaining)
    if kind == "3d":
        subject = f"⏰ {what} заканчивается через 3 дня"
        title = f"{what} заканчивается через 3 дня"
        intro = (
            f"{_user_display_name(user)}, напоминаем: доступ к VPN закончится {fmt_dt_msk(expires_at)}. "
            "Продлите заранее, чтобы соединение не прервалось в неподходящий момент."
        )
        tone, badge = "warn", "Напоминание"
    else:
        subject = f"⚠️ {what} закончится через {left}"
        title = f"До конца осталось {left}"
        intro = (
            f"{_user_display_name(user)}, {what.lower()} истекает совсем скоро — {fmt_dt_msk(expires_at)}. "
            "После этого VPN перестанет работать на всех устройствах."
        )
        tone, badge = "danger", "Последнее напоминание"
    ok, err = await send_branded_email(
        email,
        subject=subject,
        title=title,
        intro=intro,
        rows=[EmailRow("Окончание", fmt_dt_msk(expires_at)), EmailRow("Осталось", left)],
        button_text="Продлить подписку",
        button_url=site_url(settings, "/app/subscription") or None,
        note="Продлить можно на сайте или в Telegram-боте в разделе «Моя подписка».",
        tone=tone,
        badge=badge,
        settings=settings,
    )
    if not ok:
        logger.warning("expiry email %s failed user=%s: %s", kind, user.id, err)
    return ok


_PROVIDER_LABELS = {"cryptobot": "CryptoBot", "platega": "Platega", "balance": "Баланс"}


async def send_topup_receipt_email(
    *,
    user: User,
    settings: Settings,
    amount_rub: Decimal,
    balance_after: Decimal | None = None,
    promo_bonus_rub: Decimal | None = None,
    first_topup_extra_rub: Decimal | None = None,
    provider_name: str | None = None,
    transaction_id: int | None = None,
) -> bool:
    """Чек о пополнении баланса. Транзакционное письмо — отправляется независимо от согласия на рассылку."""
    email = user_email_for_notify(user, settings)
    if email is None:
        return False
    rows = [EmailRow("Зачислено", f"{amount_rub} ₽")]
    if promo_bonus_rub is not None and promo_bonus_rub > 0:
        rows.append(EmailRow("В т.ч. бонус промокода", f"+{promo_bonus_rub} ₽"))
    if first_topup_extra_rub is not None and first_topup_extra_rub > 0:
        rows.append(EmailRow("В т.ч. бонус первого пополнения", f"+{first_topup_extra_rub} ₽"))
    if balance_after is not None:
        rows.append(EmailRow("Баланс сейчас", f"{balance_after} ₽"))
    if provider_name:
        rows.append(EmailRow("Способ оплаты", _PROVIDER_LABELS.get(provider_name.lower(), provider_name)))
    if transaction_id:
        rows.append(EmailRow("Номер операции", f"#{transaction_id}"))
    rows.append(EmailRow("Дата", fmt_dt_msk(datetime.now(timezone.utc))))
    ok, err = await send_branded_email(
        email,
        subject=f"🧾 Баланс пополнен на {amount_rub} ₽",
        title="Баланс пополнен",
        intro=f"{_user_display_name(user)}, оплата прошла успешно — средства уже на балансе.",
        rows=rows,
        button_text="Открыть личный кабинет",
        button_url=site_url(settings, "/app") or None,
        note="Это электронная квитанция о зачислении средств. Сохраните её на случай вопросов к поддержке.",
        tone="success",
        badge="Чек об оплате",
        settings=settings,
    )
    if not ok:
        logger.warning("topup receipt email failed user=%s: %s", user.id, err)
    return ok
