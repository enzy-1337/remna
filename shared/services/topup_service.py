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
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.subscription import Subscription
from shared.services.referral_service import grant_referrer_reward_from_topup

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
    if amount_rub < settings.billing_min_topup_rub:
        raise ValueError(f"Минимальная сумма пополнения: {settings.billing_min_topup_rub} ₽")
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

    # Приветственный бонус на первом пополнении пользователя без активной подписки:
    # отмечаем в транзакциях и пробуем дать +5 ГБ в Remnawave, если аккаунт уже существует.
    first_topup = (
        await session.execute(
            select(Transaction.id)
            .where(
                Transaction.user_id == user.id,
                Transaction.type == "topup",
                Transaction.status == "completed",
                Transaction.id != txn.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none() is None
    has_active_sub = (
        await session.execute(
            select(Subscription.id)
            .where(
                Subscription.user_id == user.id,
                Subscription.status.in_(("active", "trial")),
                Subscription.expires_at > datetime.now(timezone.utc),
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None
    if first_topup and not has_active_sub:
        bonus_payment_id = f"welcome_gb_bonus:{user.id}"
        already_bonus = (
            await session.execute(
                select(Transaction.id)
                .where(Transaction.user_id == user.id, Transaction.payment_id == bonus_payment_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if already_bonus is None:
            session.add(
                Transaction(
                    user_id=user.id,
                    type="welcome_gb_bonus",
                    amount=Decimal("0"),
                    currency="RUB",
                    payment_provider="billing_v2",
                    payment_id=bonus_payment_id,
                    status="completed",
                    description="Стартовый бонус 5 ГБ после первого пополнения",
                    meta={"bonus_gb": 5},
                )
            )
            if user.remnawave_uuid is not None:
                rw = RemnaWaveClient(settings)
                try:
                    uinfo = await rw.get_user(str(user.remnawave_uuid))
                    current = int(uinfo.get("trafficLimitBytes") or 0)
                    new_limit = current + 5 * (1024**3)
                    await rw.update_user(str(user.remnawave_uuid), traffic_limit_bytes=new_limit)
                except RemnaWaveError:
                    logger.warning("welcome 5GB bonus: failed to update rw user user_id=%s", user.id)

    await grant_referrer_reward_from_topup(
        session,
        referred_user=user,
        topup_amount_rub=credited,
        settings=settings,
        internal_topup_txn_id=txn.id,
    )

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
    internal_transaction_id: int | None = None,
) -> None:
    tg: int | None = int(telegram_id) if telegram_id else None
    if (tg is None or tg <= 0) and user_id is not None:
        factory0 = get_session_factory()
        async with factory0() as s0:
            u0 = await s0.get(User, user_id)
            if u0 is not None:
                tg = int(u0.telegram_id)

    extra: str | None = None
    invoice_mid: int | None = None
    factory = get_session_factory()
    async with factory() as session:
        if internal_transaction_id is not None:
            try:
                txn = await session.get(Transaction, int(internal_transaction_id))
                if txn is not None and txn.meta and txn.user_id == user_id:
                    im = txn.meta.get("invoice_message_id")
                    if isinstance(im, int):
                        invoice_mid = im
                    elif isinstance(im, str) and im.isdigit():
                        invoice_mid = int(im)
            except Exception:
                logger.debug("notify_topup_success: cannot load invoice message id", exc_info=True)
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
        if invoice_mid is not None:
            from shared.services.telegram_notify import delete_telegram_message

            deleted = await delete_telegram_message(tg, invoice_mid, settings=settings)
            if not deleted:
                logger.debug(
                    "notify_topup_success: invoice message not deleted user_tg=%s mid=%s",
                    tg,
                    invoice_mid,
                )
        mid = await send_telegram_message(
            tg,
            text,
            settings=settings,
            reply_markup={
                "inline_keyboard": [
                    [{"text": "💰 Баланс", "callback_data": "menu:balance"}],
                ]
            },
        )
        if mid is None:
            logger.warning("notify_topup_success: не удалось отправить сообщение user_tg=%s", tg)

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
