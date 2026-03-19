"""Создание pending-транзакций пополнения и зачисление по вебхуку (идемпотентно)."""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.md2 import bold, join_lines, plain
from shared.models.transaction import Transaction
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
) -> tuple[str, int | None, Decimal | None, int | None]:
    """
    Идемпотентное зачисление.
    Возвращает (status, telegram_id, сумма_зачисления_₽, user_id при status=completed).
    status: completed | duplicate | rejected | not_found
    """
    r = await session.execute(select(Transaction).where(Transaction.id == parsed.internal_transaction_id))
    txn = r.scalar_one_or_none()
    if txn is None:
        logger.warning("topup webhook: txn id=%s not found", parsed.internal_transaction_id)
        return "not_found", None, None, None

    if txn.type != "topup":
        return "rejected", None, None, None

    if (txn.payment_provider or "").lower() != provider_name.lower():
        logger.warning("topup webhook: provider mismatch txn=%s expected=%s got=%s", txn.id, txn.payment_provider, provider_name)
        return "rejected", None, None, None

    if txn.status == "completed":
        return "duplicate", None, None, None

    if txn.payment_id and parsed.external_payment_id and txn.payment_id != parsed.external_payment_id:
        logger.warning(
            "topup webhook: external id mismatch txn=%s db=%s hook=%s",
            txn.id,
            txn.payment_id,
            parsed.external_payment_id,
        )
        return "rejected", None, None, None

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
        return "not_found", None, None, None

    user.balance += credited
    txn.status = "completed"
    meta = dict(txn.meta or {})
    meta["webhook_parsed"] = {
        "external_payment_id": parsed.external_payment_id,
        "amount_rub_hook": str(parsed.amount_rub) if parsed.amount_rub else None,
    }
    txn.meta = meta

    tg_id = int(meta.get("telegram_id") or 0)
    await session.flush()

    return "completed", (tg_id if tg_id else None), credited, user.id


async def notify_topup_success(
    *,
    telegram_id: int,
    amount_rub: Decimal,
    settings: Settings,
    user_id: int | None = None,
    provider_name: str | None = None,
) -> None:
    extra: str | None = None
    factory = get_session_factory()
    async with factory() as session:
        extra = await try_apply_smart_cart_after_topup(session, telegram_id, settings)
        await session.commit()
    text = plain("✅ Баланс пополнен на ") + bold(str(amount_rub)) + plain(" ₽.")
    if extra:
        text += f"\n\n{extra}"
    await send_telegram_message(telegram_id, text, settings=settings)

    if user_id is not None:
        from shared.services.admin_notify import notify_admin

        factory2 = get_session_factory()
        async with factory2() as session2:
            u = await session2.get(User, user_id)
            if u is not None:
                await notify_admin(
                    settings,
                    title="💳 " + bold("Пополнение баланса"),
                    lines=[
                        f"Сумма: {bold(str(amount_rub))} ₽",
                        f"Провайдер: {bold(provider_name or '—')}",
                    ],
                    event_type="topup",
                    subject_user=u,
                    session=session2,
                )
            await session2.commit()
