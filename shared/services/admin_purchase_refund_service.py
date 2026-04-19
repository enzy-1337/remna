"""Отмена покупок тарифа или слота устройства с баланса (web-admin)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.models.plan import Plan
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.device import Device
from shared.models.subscription import Subscription
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.subscription_service import (
    MIN_DEVICES,
    admin_disable_subscription_record,
    get_active_subscription,
)
from shared.services.remnawave_user_panel_sync import update_rw_user_respecting_hwid_limit

logger = logging.getLogger(__name__)


def _payment_is_balance(txn: Transaction) -> bool:
    """Покупки тарифа/слота с баланса: payment_provider balance (без учёта регистра) или пусто в старых записях."""
    if txn.type not in ("subscription", "manual_add"):
        return False
    pp = (txn.payment_provider or "").strip().lower()
    if pp == "balance":
        return True
    # Старые строки могли не заполнять провайдер — для типов покупки из бота считаем балансом
    return pp == ""


def _parse_iso_utc(s: str) -> datetime:
    x = (s or "").strip()
    if x.endswith("Z"):
        x = x[:-1] + "+00:00"
    dt = datetime.fromisoformat(x)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _subtract_pack_days_from_subscription(
    session: AsyncSession,
    *,
    user: User,
    sub: Subscription,
    meta: dict,
    settings: Settings,
) -> tuple[bool, str]:
    """Забирает добавленный тарифом период: новый expires = старый − duration_days плана."""
    plan_ref = meta.get("purchased_plan_id") or meta.get("plan_id")
    dur_raw = meta.get("duration_days")
    plan: Plan | None = None
    if plan_ref is not None:
        try:
            plan = await session.get(Plan, int(plan_ref))
        except (TypeError, ValueError):
            plan = None
    dd: int | None = None
    if dur_raw is not None:
        try:
            dd = int(dur_raw)
        except (TypeError, ValueError):
            dd = None
    if dd is None and plan is not None:
        dd = int(plan.duration_days)
    if not dd or dd <= 0:
        return False, "Нет duration_days / plan_id в метаданных — нельзя вычесть купленные дни."

    now = datetime.now(timezone.utc)
    exp = sub.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    new_exp = exp - timedelta(days=dd)
    if sub.started_at:
        st = sub.started_at if sub.started_at.tzinfo else sub.started_at.replace(tzinfo=timezone.utc)
        if new_exp < st:
            new_exp = st
    sub.expires_at = new_exp
    if new_exp <= now and sub.status in ("active", "trial"):
        sub.status = "cancelled"
        if user.remnawave_uuid is not None and not settings.remnawave_stub:
            rw = RemnaWaveClient(settings)
            try:
                await rw.update_user(str(user.remnawave_uuid), status="DISABLED")
            except RemnaWaveError as e:
                logger.warning("subtract_pack_days RW disable: %s", e)
    elif user.remnawave_uuid is not None and not settings.remnawave_stub:
        rw = RemnaWaveClient(settings)
        try:
            await update_rw_user_respecting_hwid_limit(
                rw,
                str(user.remnawave_uuid),
                devices_limit_for_panel=sub.devices_count,
                expire_at=sub.expires_at,
                status="ACTIVE",
            )
        except RemnaWaveError as e:
            logger.warning("subtract_pack_days RW: %s", e)
            return False, f"Панель VPN: {e}"
    return True, ""


async def admin_refund_purchase_transaction(
    session: AsyncSession,
    *,
    user_id: int,
    txn_id: int,
    settings: Settings,
) -> tuple[bool, str]:
    txn = await session.get(Transaction, txn_id)
    if txn is None or txn.user_id != user_id:
        return False, "Транзакция не найдена."
    if txn.status != "completed":
        return False, "Можно отменить только завершённые списания."
    meta = dict(txn.meta or {})
    if meta.get("refunded"):
        return False, "Эта операция уже была отменена."

    if txn.type == "usage_charge":
        return False, "Списания PAYG нельзя отменить через эту кнопку."
    if txn.type not in ("subscription", "manual_add"):
        return False, "Отмена доступна только для покупки тарифа или слота устройства с баланса."
    if not _payment_is_balance(txn):
        return False, "Доступен возврат только за списания с баланса (payment_provider balance)."

    user = await session.get(User, user_id)
    if user is None:
        return False, "Пользователь не найден."

    amt = txn.amount
    if amt is None or amt <= 0:
        return False, "Некорректная сумма транзакции."

    if txn.type == "manual_add":
        return await _refund_manual_add(session, user=user, txn=txn, settings=settings, meta=meta, amount=amt)
    return await _refund_subscription(session, user=user, txn=txn, settings=settings, meta=meta, amount=amt)


async def _refund_manual_add(
    session: AsyncSession,
    *,
    user: User,
    txn: Transaction,
    settings: Settings,
    meta: dict,
    amount: Decimal,
) -> tuple[bool, str]:
    device_id = meta.get("device_id")
    sub_id = meta.get("subscription_id")
    if device_id is None or sub_id is None:
        return False, "В записи нет device_id/subscription_id (старая транзакция) — отмена недоступна."

    sub = await session.get(Subscription, int(sub_id))
    if sub is None or sub.user_id != user.id:
        return False, "Подписка для этой покупки не найдена."

    dev = await session.get(Device, int(device_id))
    if dev is None or dev.subscription_id != sub.id or dev.user_id != user.id:
        return False, "Запись устройства не найдена или не совпадает с покупкой."

    if sub.devices_count <= MIN_DEVICES:
        return False, "Нельзя уменьшить число слотов ниже минимума после этой покупки."

    rw = RemnaWaveClient(settings)
    new_limit = sub.devices_count - 1
    if user.remnawave_uuid is not None and not settings.remnawave_stub:
        try:
            await update_rw_user_respecting_hwid_limit(
                rw,
                str(user.remnawave_uuid),
                devices_limit_for_panel=new_limit,
            )
        except RemnaWaveError as e:
            return False, f"Панель VPN: {e}"

    await session.delete(dev)
    sub.devices_count = new_limit
    user.balance += amount

    refund = Transaction(
        user_id=user.id,
        type="purchase_refund",
        amount=amount,
        currency="RUB",
        payment_provider="admin_refund",
        payment_id=f"refund:{txn.id}",
        status="completed",
        description=f"Возврат за доп. устройство (отмена txn #{txn.id})",
        meta={"reverses_txn_id": txn.id, "kind": "manual_add"},
    )
    session.add(refund)
    txn.status = "refunded"
    await session.flush()
    meta["refunded_at"] = datetime.now(timezone.utc).isoformat()
    meta["refund_txn_id"] = refund.id
    txn.meta = meta
    await session.flush()

    if user.billing_mode == "hybrid" and settings.billing_v2_enabled:
        from shared.services.billing_v2.balance_floor_panel_service import sync_hybrid_balance_floor_panel_state

        await sync_hybrid_balance_floor_panel_state(session, user, settings)

    return True, "Слот снят, сумма возвращена на баланс."


async def _refund_subscription(
    session: AsyncSession,
    *,
    user: User,
    txn: Transaction,
    settings: Settings,
    meta: dict,
    amount: Decimal,
) -> tuple[bool, str]:
    pk = meta.get("purchase_kind")
    sub_db_id = meta.get("subscription_db_id_after")

    sub: Subscription | None = None
    if sub_db_id is not None:
        sub = await session.get(Subscription, int(sub_db_id))
    if sub is None or sub.user_id != user.id:
        sub = await get_active_subscription(session, user.id)
    if sub is None or sub.user_id != user.id:
        return False, "Подписка для этой покупки не найдена."

    if pk == "new":
        ok, msg = await admin_disable_subscription_record(
            session,
            user_id=user.id,
            subscription_id=sub.id,
            settings=settings,
        )
        if not ok:
            return False, str(msg)

    elif pk == "extend":
        exp_s = meta.get("expires_at_before")
        if exp_s:
            try:
                prev_exp = _parse_iso_utc(str(exp_s))
            except ValueError:
                return False, "Некорректная дата в метаданных."
            sub.expires_at = prev_exp
            if meta.get("devices_count_before") is not None:
                try:
                    sub.devices_count = int(meta["devices_count_before"])
                except (TypeError, ValueError):
                    pass
            if user.remnawave_uuid is not None and not settings.remnawave_stub:
                rw = RemnaWaveClient(settings)
                try:
                    await update_rw_user_respecting_hwid_limit(
                        rw,
                        str(user.remnawave_uuid),
                        devices_limit_for_panel=sub.devices_count,
                        expire_at=sub.expires_at,
                        status="ACTIVE",
                    )
                except RemnaWaveError as e:
                    logger.warning("refund_subscription extend RW: %s", e)
                    return False, f"Панель VPN: {e}"
        else:
            ok_dd, err_dd = await _subtract_pack_days_from_subscription(
                session, user=user, sub=sub, meta=meta, settings=settings
            )
            if not ok_dd:
                return False, err_dd

    elif pk in (None, "") and txn.type == "subscription":
        ok_dd, err_dd = await _subtract_pack_days_from_subscription(
            session, user=user, sub=sub, meta=meta, settings=settings
        )
        if not ok_dd:
            return False, err_dd

    else:
        return False, "Нет данных отката в метаданных (нужны purchase_kind или plan_id/duration_days)."

    user.balance += amount
    refund = Transaction(
        user_id=user.id,
        type="purchase_refund",
        amount=amount,
        currency="RUB",
        payment_provider="admin_refund",
        payment_id=f"refund:{txn.id}",
        status="completed",
        description=f"Возврат за тариф (отмена txn #{txn.id})",
        meta={"reverses_txn_id": txn.id, "kind": "subscription", "purchase_kind": pk},
    )
    session.add(refund)
    txn.status = "refunded"
    await session.flush()
    meta["refunded_at"] = datetime.now(timezone.utc).isoformat()
    meta["refund_txn_id"] = refund.id
    txn.meta = meta
    await session.flush()

    if user.billing_mode == "hybrid" and settings.billing_v2_enabled:
        from shared.services.billing_v2.balance_floor_panel_service import sync_hybrid_balance_floor_panel_state

        await sync_hybrid_balance_floor_panel_state(session, user, settings)

    return True, "Покупка тарифа отменена, сумма возвращена на баланс."


def txn_row_refund_eligible(txn: Transaction) -> bool:
    if txn.status != "completed" or not _payment_is_balance(txn):
        return False
    if txn.type == "manual_add":
        m = txn.meta or {}
        return bool(m.get("device_id") and m.get("subscription_id"))
    if txn.type == "subscription":
        m = txn.meta or {}
        if m.get("purchase_kind") in ("new", "extend"):
            return True
        # Покупка с баланса: всегда есть plan_id в meta — откат по вычитанию дней
        return bool(m.get("purchased_plan_id") or m.get("plan_id"))
    return False
