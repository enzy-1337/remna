"""Раздел «Моя подписка»: статус, покупка/продление, ссылка, авто-продление."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from shared.config import get_settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.user import User
from shared.services.subscription_service import (
    get_active_subscription,
    list_paid_plans,
    purchase_plan_with_balance,
    set_subscription_auto_renew,
)

logger = logging.getLogger(__name__)

router = Router(name="subscription")


def _subscription_actions_markup(*, has_active: bool, auto_renew: bool):
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📦 Тарифы / продлить", callback_data="sub:plans"))
    b.row(InlineKeyboardButton(text="🔗 Ссылка подписки", callback_data="sub:link"))
    if has_active:
        ar_text = "⏸ Выключить авто-продление" if auto_renew else "▶️ Включить авто-продление"
        b.row(InlineKeyboardButton(text=ar_text, callback_data="sub:toggle_ar"))
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main"))
    return b.as_markup()


async def _screen_text(session: AsyncSession, user: User) -> str:
    sub = await get_active_subscription(session, user.id)
    now = datetime.now(timezone.utc)
    if not sub:
        return (
            "🔑 <b>Моя подписка</b>\n\n"
            "Сейчас нет активной подписки.\n"
            "Выберите тариф или активируйте триал в главном меню."
        )
    plan_name = html.escape(sub.plan.name) if sub.plan else "—"
    st = html.escape(sub.status)
    exp = sub.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    left = exp - now
    days = max(0, left.days)
    ar = "да" if sub.auto_renew else "нет"
    trial_note = ""
    if sub.status == "trial":
        trial_note = (
            "\n\nℹ️ Активен <b>триал</b>. Покупка тарифа добавит срок от текущей даты окончания."
        )
    return (
        f"🔑 <b>Моя подписка</b>\n\n"
        f"Тариф: <b>{plan_name}</b>\n"
        f"Статус: <b>{st}</b>\n"
        f"Истекает: <b>{exp.strftime('%d.%m.%Y %H:%M')} UTC</b>\n"
        f"Осталось дней (оценка): <b>{days}</b>\n"
        f"Слотов устройств: <b>{sub.devices_count}</b>\n"
        f"Авто-продление: <b>{ar}</b>{trial_note}"
    )


@router.callback_query(F.data == "menu:subscription")
async def cb_subscription_home(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    sub = await get_active_subscription(session, db_user.id)
    text = await _screen_text(session, db_user)
    kb = _subscription_actions_markup(
        has_active=sub is not None,
        auto_renew=sub.auto_renew if sub else False,
    )
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "sub:plans")
async def cb_plans(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    plans = await list_paid_plans(session)
    if not plans:
        await cq.answer("Нет доступных тарифов", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for p in plans:
        b.row(
            InlineKeyboardButton(
                text=f"{p.name} — {p.price_rub} ₽",
                callback_data=f"sub:buy:{p.id}",
            )
        )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:subscription"))
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(
            "📦 <b>Выберите тариф</b>\n\n"
            "Оплата с баланса. Если средств не хватает — тариф сохранится в корзине на 30 мин.",
            reply_markup=b.as_markup(),
        )


@router.callback_query(F.data.startswith("sub:buy:"))
async def cb_buy_plan(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    try:
        pid = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Ошибка тарифа", show_alert=True)
        return
    settings = get_settings()
    tid = cq.from_user.id if cq.from_user else db_user.telegram_id
    ok, msg, kind = await purchase_plan_with_balance(
        session,
        user=db_user,
        plan_id=pid,
        telegram_id=tid,
        settings=settings,
        save_to_cart_if_insufficient=True,
    )
    await cq.answer()
    if not cq.message:
        return
    if ok:
        body = msg + "\n\n" + await _screen_text(session, db_user)
        sub = await get_active_subscription(session, db_user.id)
        await cq.message.edit_text(
            body,
            reply_markup=_subscription_actions_markup(
                has_active=sub is not None,
                auto_renew=sub.auto_renew if sub else False,
            ),
        )
    else:
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text="⬅️ К тарифам", callback_data="sub:plans"))
        b.row(InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance"))
        b.row(InlineKeyboardButton(text="🔑 Подписка", callback_data="menu:subscription"))
        await cq.message.edit_text(msg, reply_markup=b.as_markup())


@router.callback_query(F.data == "sub:link")
async def cb_sub_link(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    if not db_user.remnawave_uuid:
        await cq.answer("Сначала активируйте триал или купите подписку.", show_alert=True)
        return
    settings = get_settings()
    rw = RemnaWaveClient(settings)
    try:
        u = await rw.get_user(str(db_user.remnawave_uuid))
    except RemnaWaveError:
        logger.exception("get_user for subscription link")
        await cq.answer("Не удалось получить ссылку.", show_alert=True)
        return
    url = u.get("subscriptionUrl") or ""
    if not url:
        await cq.answer("Ссылка пуста в панели.", show_alert=True)
        return
    href = html.escape(url, quote=True)
    text = f"🔗 <b>Ссылка подписки</b>\n\n<a href=\"{href}\">Открыть / скопировать в приложении</a>"
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:subscription"))
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(text, reply_markup=b.as_markup())


@router.callback_query(F.data == "sub:toggle_ar")
async def cb_toggle_ar(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    sub = await get_active_subscription(session, db_user.id)
    if not sub:
        await cq.answer("Нет активной подписки.", show_alert=True)
        return
    new_val = not sub.auto_renew
    ok, tip = await set_subscription_auto_renew(session, db_user.id, new_val)
    if not ok:
        await cq.answer(tip, show_alert=True)
        return
    await cq.answer(tip)
    sub2 = await get_active_subscription(session, db_user.id)
    text = await _screen_text(session, db_user)
    if cq.message:
        await cq.message.edit_text(
            text,
            reply_markup=_subscription_actions_markup(
                has_active=sub2 is not None,
                auto_renew=sub2.auto_renew if sub2 else False,
            ),
        )
