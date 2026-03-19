"""Экран и ввод промокода."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.keyboards.inline import submenu_back_keyboard
from bot.states.promo import PromoStates
from shared.config import get_settings
from shared.models.user import User
from shared.services.admin_notify import notify_admin
from shared.services.promo_service import apply_promo_code_for_user

router = Router(name="promo")


def _promo_cancel_keyboard() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="promo:cancel"))
    return b


@router.callback_query(F.data == "menu:promo")
async def cb_promo_open(cq: CallbackQuery, db_user: User | None, state: FSMContext) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    await state.set_state(PromoStates.waiting_code)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(
            "🎁 <b>Промокод</b>\n\nВведите код одним сообщением.",
            reply_markup=_promo_cancel_keyboard().as_markup(),
        )


@router.callback_query(F.data == "promo:cancel")
async def cb_promo_cancel(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(
            "Операция отменена.",
            reply_markup=submenu_back_keyboard(),
        )


@router.message(PromoStates.waiting_code, F.text)
async def msg_promo_code(
    message: Message,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await reject_if_blocked(message, db_user) or db_user is None:
        await state.clear()
        return

    ok, text, meta = await apply_promo_code_for_user(
        session,
        user=db_user,
        raw_code=message.text or "",
    )
    await state.clear()
    if ok:
        if meta:
            settings = get_settings()
            await notify_admin(
                settings,
                title="🎁 <b>Промокод применён</b>",
                lines=[
                    f"Код: <code>{html.escape(meta['code'])}</code>",
                    f"Тип: <code>{html.escape(meta['type'])}</code>",
                    f"Сумма: <b>{html.escape(meta['value'])}</b> ₽",
                ],
                event_type="promo_apply",
                subject_user=db_user,
                session=session,
            )
        await message.answer(text, reply_markup=submenu_back_keyboard())
    else:
        await message.answer(f"❌ {text}", reply_markup=submenu_back_keyboard())
