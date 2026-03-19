"""Раздел «Устройства»: список, платное добавление слота, удаление."""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from shared.config import get_settings
from shared.models.user import User
from shared.services.subscription_service import (
    MAX_DEVICES,
    MIN_DEVICES,
    add_paid_device_slot,
    get_active_subscription,
    list_user_devices,
    remove_device_slot,
)

logger = logging.getLogger(__name__)

router = Router(name="devices")


def _devices_kb(devices: list, slots: int, price_label: str):
    b = InlineKeyboardBuilder()
    for d in devices:
        nm = html.escape(d.name)[:34]
        b.row(InlineKeyboardButton(text=f"🗑 {nm}", callback_data=f"dev:ask:{d.id}"))
    if slots < MAX_DEVICES:
        b.row(
            InlineKeyboardButton(
                text=f"➕ Добавить слот ({price_label} ₽)",
                callback_data="dev:add",
            )
        )
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main"))
    return b.as_markup()


async def _render_devices(session: AsyncSession, user: User) -> tuple[str, object]:
    settings = get_settings()
    sub = await get_active_subscription(session, user.id)
    if not sub:
        kb = (
            InlineKeyboardBuilder()
            .row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main"))
            .as_markup()
        )
        return "🖥️ <b>Устройства</b>\n\nСначала оформите подписку или триал.", kb

    devices = await list_user_devices(session, sub.id)
    lines = [
        "🖥️ <b>Устройства</b>\n",
        f"Слотов: <b>{sub.devices_count}</b> (мин. {MIN_DEVICES}, макс. {MAX_DEVICES})\n",
    ]
    if devices:
        lines.append("")
        for d in devices:
            lines.append(f"• {html.escape(d.name)}")
    else:
        lines.append("\n<i>Записей пока нет.</i>")
    price = str(settings.extra_device_price_rub)
    body = "\n".join(lines)
    return body, _devices_kb(devices, sub.devices_count, price)


@router.callback_query(F.data == "menu:devices")
async def cb_devices_home(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    text, kb = await _render_devices(session, db_user)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "dev:add")
async def cb_dev_add(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    ok, msg = await add_paid_device_slot(session, user=db_user, settings=settings)
    if ok:
        from shared.services.admin_notify import notify_admin

        await notify_admin(
            settings,
            title="🖥 <b>Куплен слот устройства</b>",
            lines=[
                f"Списано: <b>{settings.extra_device_price_rub}</b> ₽",
            ],
            event_type="extra_device_purchase",
            subject_user=db_user,
            session=session,
        )
    if not ok:
        await cq.answer(msg.replace("<b>", "").replace("</b>", ""), show_alert=True)
        return
    await cq.answer()
    text, kb = await _render_devices(session, db_user)
    if cq.message:
        await cq.message.edit_text(text + "\n\n" + msg, reply_markup=kb)


@router.callback_query(F.data.startswith("dev:ask:"))
async def cb_dev_ask_delete(
    cq: CallbackQuery,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    try:
        did = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Ошибка", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Удалить", callback_data=f"dev:do:{did}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="menu:devices"),
    )
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(
            "🗑 Удалить это устройство из списка?\n"
            "(Слот в панели уменьшится; минимум 2 устройства на аккаунт.)",
            reply_markup=b.as_markup(),
        )


@router.callback_query(F.data.startswith("dev:do:"))
async def cb_dev_confirm_delete(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    try:
        did = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Ошибка", show_alert=True)
        return
    settings = get_settings()
    ok, msg = await remove_device_slot(session, user=db_user, device_id=did, settings=settings)
    if not ok:
        await cq.answer(msg.replace("<b>", "").replace("</b>", ""), show_alert=True)
        return
    await cq.answer()
    text, kb = await _render_devices(session, db_user)
    if cq.message:
        await cq.message.edit_text(text + "\n\n" + msg, reply_markup=kb)
