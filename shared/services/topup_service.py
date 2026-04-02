"""Создание pending-транзакций пополнения и зачисление по вебхуку (идемпотентно)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.md2 import bold, join_lines, plain
from shared.models.transaction import Transaction
from shared.models.promo import PromoCode, PromoUsage
from shared.models.user import User
from shared.payments.base import ParsedWebhookTopup
from shared.payments.registry import get_payment_provider
from shared.database import get_session_factory
from shared.services.smart_cart import clear_cart, get_cart
from shared.services.telegram_notify import send_telegram_message

logger = logging.getLogger(__name__)


async def try_apply_smart_cart_after_topup(
    session: AsyncSession,
    telegram_id: int,
    settings: Settings,
) -> str | None:
    """После пополнения баланса — попытка купить тариф из Redis-корзины."""
    from shared.services.subscription_service import purchase_plan_with_balance
    from shared.services.user_registration import get_user_by_telegram_id

    cart = await get_cart(telegram_id, settings)
    if not cart:
        return None
    user = await get_user_by_telegram_id(session, telegram_id)
    if not user:
        await clear_cart(telegram_id, settings)
        return None
    try:
        plan_id = int(cart["plan_id"])
    except (TypeError, ValueError, KeyError):
        await clear_cart(telegram_id, settings)
        return plain("Корзина устарела — выберите тариф снова в «Моя подписка».")

    ok, msg, kind = await purchase_plan_with_balance(
        session,
        user=user,
        plan_id=plan_id,
        telegram_id=telegram_id,
        settings=settings,
        save_to_cart_if_insufficient=False,
    )
    if kind == "success":
        await clear_cart(telegram_id, settings)
        return join_lines(plain("🛒 ") + bold("Автопокупка из корзины"), msg)
    if kind == "insufficient":
        return join_lines(
            plain("🛒 В корзине тариф, но средств всё ещё не хватает:"),
            msg,
        )
    return join_lines(plain("🛒 Не удалось оформить корзину:"), msg)


async def create_topup_payment(
    session: AsyncSession,
    *,
    user: User,
    telegram_id: int,
    amount_rub: Decimal,
    provider_name: str,
    settings: Settings,
) -> tuple[Transaction, str]:
    """
    Создаёт транзакцию pending и счёт у провайдера. Возвращает (txn, pay_url).
    """
    prov = get_payment_provider(provider_name, settings)
    txn = Transaction(
        user_id=user.id,
        type="topup",
        amount=amount_rub,
        currency="RUB",
        payment_provider=provider_name,
        payment_id=None,
        status="pending",
        description=f"Пополнение баланса через {provider_name}",
        meta={"telegram_id": telegram_id},
    )
    session.add(txn)
    await session.flush()

    result = await prov.create_topup_invoice(
        amount_rub=amount_rub,
        internal_transaction_id=txn.id,
        description=txn.description or "Пополнение VPN",
    )
    txn.payment_id = result.external_payment_id
    meta = dict(txn.meta or {})
    meta["pay_url"] = result.pay_url
    meta["create_raw"] = result.raw
    txn.meta = meta
    await session.flush()
    return txn, result.pay_url


async def apply_topup_from_webhook(
    session: AsyncSession,
    *,
    provider_name: str,
    parsed: ParsedWebhookTopup,
    settings: Settings,
) -> tuple[str, int | None, Decimal | None, int | None, Decimal | None]:
    """
    Идемпотентное зачисление.
    Возвращает (status, telegram_id, сумма_зачисления_₽, user_id при status=completed).
    status: completed | duplicate | rejected | not_found
    """
    r = await session.execute(select(Transaction).where(Transaction.id == parsed.internal_transaction_id))
    txn = r.scalar_one_or_none()
    if txn is None:
        logger.warning("topup webhook: txn id=%s not found", parsed.internal_transaction_id)
        return "not_found", None, None, None, None

    if txn.type != "topup":
        return "rejected", None, None, None, None

    if (txn.payment_provider or "").lower() != provider_name.lower():
        logger.warning("topup webhook: provider mismatch txn=%s expected=%s got=%s", txn.id, txn.payment_provider, provider_name)
        return "rejected", None, None, None, None

    if txn.status == "completed":
        return "duplicate", None, None, None, None

    if txn.payment_id and parsed.external_payment_id and txn.payment_id != parsed.external_payment_id:
        logger.warning(
            "topup webhook: external id mismatch txn=%s db=%s hook=%s",
            txn.id,
            txn.payment_id,
            parsed.external_payment_id,
        )
        # Для Platega бывает разный формат идентификаторов (создание/вебхук),
        # при этом `internal_transaction_id` всё равно мапится на запись в БД.
        # Поэтому не блокируем зачисление, а фиксируем payment_id как best-effort.
        if provider_name.lower().strip() == "platega":
            txn.payment_id = parsed.external_payment_id
        else:
            return "rejected", None, None, None, None

    # Сумма: доверяем нашей записи в БД; вебхук может уточнить для fiat
    credited = txn.amount
    if parsed.amount_rub and parsed.amount_rub > 0:
        if abs(parsed.amount_rub - txn.amount) > Decimal("1.00"):
            logger.warning(
                "topup webhook: amount drift txn=%s db=%s hook=%s — используем БД",
                txn.id,
                txn.amount,
                parsed.amount_rub,
            )

    user = await session.get(User, txn.user_id)
    if user is None:
        return "not_found", None, None, None, None

    user.balance += credited

    # Применяем бонус к первому успешному пополнению после активации промокода.
    # Начисление идемпотентно: если промокод уже применялся к пополнению, это отмечено в promo_usages.
    promo_bonus_total = Decimal("0")
    now = datetime.now(timezone.utc)  # местное время для метки applied_at
    r2 = await session.execute(
        select(PromoUsage, PromoCode)
        .join(PromoCode, PromoUsage.promo_id == PromoCode.id)
        .where(
            PromoUsage.user_id == user.id,
            PromoCode.type == "topup_bonus_percent",
            PromoUsage.topup_bonus_applied_at.is_(None),
        )
    )
    for usage, promo in r2.all():
        percent = Decimal(str(promo.value))
        if percent <= 0:
            usage.topup_bonus_applied_at = now
            continue
        bonus = (credited * percent / Decimal("100")).quantize(Decimal("0.01"))
        if bonus > 0:
            user.balance += bonus
            promo_bonus_total += bonus
            session.add(
                Transaction(
                    user_id=user.id,
                    type="promo_topup_bonus",
                    amount=bonus,
                    currency="RUB",
                    payment_provider="promo",
                    payment_id=promo.code,
                    status="completed",
                    description=f"Промокод {promo.code}: бонус к пополнению (+{percent}%)",
                    meta={
                        "promo_id": promo.id,
                        "promo_type": promo.type,
                        "promo_percent": str(percent),
                        "base_topup_amount": str(credited),
                    },
                )
            )
        usage.topup_bonus_applied_at = now
    txn.status = "completed"
    meta = dict(txn.meta or {})
    meta["webhook_parsed"] = {
        "external_payment_id": parsed.external_payment_id,
        "amount_rub_hook": str(parsed.amount_rub) if parsed.amount_rub else None,
    }
    txn.meta = meta

    tg_id = int(meta.get("telegram_id") or 0)
    await session.flush()

    credited_total = credited + promo_bonus_total
    return "completed", (tg_id if tg_id else None), credited_total, user.id, promo_bonus_total


async def notify_topup_success(
    *,
    telegram_id: int | None,
    amount_rub: Decimal,
    promo_bonus_rub: Decimal | None = None,
    settings: Settings,
    user_id: int | None = None,
    provider_name: str | None = None,
) -> None:
    tg: int | None = int(telegram_id) if telegram_id else None
    if (tg is None or tg <= 0) and user_id is not None:
        factory0 = get_session_factory()
        async with factory0() as s0:
            u0 = await s0.get(User, user_id)
            if u0 is not None:
                tg = int(u0.telegram_id)

    extra: str | None = None
    factory = get_session_factory()
    async with factory() as session:
        if tg is not None and tg > 0:
            extra = await try_apply_smart_cart_after_topup(session, tg, settings)
        else:
            logger.warning(
                "notify_topup_success: нет telegram_id для корзины user_id=%s",
                user_id,
            )
        await session.commit()
    text = plain("✅ Баланс пополнен на ") + bold(str(amount_rub)) + plain(" ₽.")
    if extra:
        text += f"\n\n{extra}"
    if promo_bonus_rub is not None and promo_bonus_rub > 0:
        text += f"\n\n🎁 Промокод бонус: +{bold(str(promo_bonus_rub))} ₽."
    if tg is not None and tg > 0:
        await send_telegram_message(tg, text, settings=settings)

    if user_id is not None:
        from shared.services.admin_notify import notify_admin

        factory2 = get_session_factory()
        async with factory2() as session2:
            u = await session2.get(User, user_id)
            if u is not None:
                from shared.services.admin_log_topics import AdminLogTopic

                admin_lines: list[str] = [
                    f"Сумма: {bold(str(amount_rub))} ₽",
                    f"Провайдер: {bold(provider_name or '—')}",
                ]
                if promo_bonus_rub is not None and promo_bonus_rub > 0:
                    admin_lines.append(f"Бонус промокодов: {bold(str(promo_bonus_rub))} ₽")

                await notify_admin(
                    settings,
                    title="💳 " + bold("Пополнение баланса"),
                    lines=admin_lines,
                    event_type="topup",
                    topic=AdminLogTopic.PAYMENTS,
                    subject_user=u,
                    session=session2,
                )
            await session2.commit()


async def manual_check_and_apply_topup(
    session: AsyncSession,
    *,
    txn_id: int,
    settings: Settings,
) -> tuple[str, Decimal | None, Decimal | None, bool]:
    """
    Ручная проверка платежа пользователем (кнопка в боте).
    Возвращает (status, credited_total_or_None, promo_bonus_or_None, should_notify).
    should_notify=True только если зачисление выполнено в этом вызове (не дубликат вебхука).
    status: completed | pending | rejected | not_found | error
    """
    r = await session.execute(select(Transaction).where(Transaction.id == txn_id))
    txn = r.scalar_one_or_none()
    if txn is None:
        return "not_found", None, None, False
    if txn.type != "topup":
        return "rejected", None, None, False
    if txn.status == "completed":
        return "completed", txn.amount, None, False

    provider = (txn.payment_provider or "").lower().strip()
    external_id = (txn.payment_id or "").strip()
    if not provider or not external_id:
        return "error", None, None, False

    try:
        if provider == "cryptobot":
            from shared.payments.cryptobot import CryptoBotProvider

            prov = CryptoBotProvider(settings)
            paid, amt, raw = await prov.is_invoice_paid(external_id)
            if not paid:
                return "pending", None, None, False
            parsed = ParsedWebhookTopup(
                internal_transaction_id=txn.id,
                external_payment_id=external_id,
                amount_rub=amt or txn.amount,
                paid=True,
            )
        elif provider in ("platega", "platega_io"):
            from shared.payments.platega import PlategaProvider

            prov = PlategaProvider(settings)
            st, amt, raw = await prov.get_transaction_status(external_id)
            if st not in ("CONFIRMED", "PAID", "SUCCESS", "COMPLETED"):
                return "pending", None, None, False
            parsed = ParsedWebhookTopup(
                internal_transaction_id=txn.id,
                external_payment_id=external_id,
                amount_rub=amt or txn.amount,
                paid=True,
            )
        else:
            # неизвестный провайдер
            return "error", None, None, False
    except Exception:
        logger.exception("manual topup check failed txn=%s provider=%s", txn.id, provider)
        return "error", None, None, False

    status, _tg_id, credited_total, _user_id, promo_bonus = await apply_topup_from_webhook(
        session,
        provider_name=provider,
        parsed=parsed,
        settings=settings,
    )
    if status == "completed":
        return "completed", credited_total, promo_bonus, True
    if status == "duplicate":
        return "completed", txn.amount, None, False
    if status in ("rejected", "not_found"):
        return status, None, None, False
    return "error", None, None, False
