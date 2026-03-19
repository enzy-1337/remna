"""Экран и ввод промокода (MarkdownV2)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.keyboards.inline import submenu_back_keyboard
from bot.states.promo import PromoStates
from bot.utils.screen_photo import answer_callback_with_photo_screen, send_profile_screen
from shared.config import get_settings
from shared.md2 import bold, code, esc, join_lines
from shared.models.user import User
from shared.services.admin_notify import notify_admin
from shared.services.promo_service import apply_promo_code_for_user

router = Router(name="promo")


@router.message(Command("promo"))
async def cmd_promo(
    message: Message,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await reject_if_blocked(message, db_user) or db_user is None:
        return
    await state.set_state(PromoStates.waiting_code)
    settings = get_settings()
    cancel_kb = _promo_cancel_keyboard()
    await send_profile_screen(
        message.bot,
        chat_id=message.chat.id,
        caption=join_lines("🎁 " + bold("Промокод"), "", "Введите код одним сообщением."),
        reply_markup=cancel_kb.as_markup(),
        settings=settings,
        delete_message=None,
    )


def _promo_cancel_keyboard() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="promo:cancel"))
    return b


@router.callback_query(F.data == "menu:promo")
async def cb_promo_open(cq: CallbackQuery, db_user: User | None, state: FSMContext) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    await state.set_state(PromoStates.waiting_code)
    settings = get_settings()
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines("🎁 " + bold("Промокод"), "", "Введите код одним сообщением."),
        reply_markup=_promo_cancel_keyboard().as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "promo:cancel")
async def cb_promo_cancel(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(
            esc("Операция отменена."),
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
    settings = get_settings()
    if ok:
        if meta:
            await notify_admin(
                settings,
                title="🎁 " + bold("Промокод применён"),
                lines=[
                    f"Код: {code(meta['code'])}",
                    f"Тип: {code(meta['type'])}",
                    f"Сумма: {bold(str(meta['value']))} ₽",
                ],
                event_type="promo_apply",
                subject_user=db_user,
                session=session,
            )
        await send_profile_screen(
            message.bot,
            chat_id=message.chat.id,
            caption=text,
            reply_markup=submenu_back_keyboard(),
            settings=settings,
            delete_message=None,
        )
    else:
        await send_profile_screen(
            message.bot,
            chat_id=message.chat.id,
            caption=join_lines("❌ " + text),
            reply_markup=submenu_back_keyboard(),
            settings=settings,
            delete_message=None,
        )
