"""Экран и ввод промокода (MarkdownV2)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.keyboards.inline import submenu_back_keyboard
from bot.states.promo import PromoStates
from bot.utils.screen_photo import (
    answer_callback_with_photo_screen,
    delete_chat_message_safe,
    delete_message_safe,
    send_profile_screen,
)
from shared.config import get_settings
from shared.md2 import bold, code, esc, join_lines, plain
from shared.models.user import User
from shared.services.admin_log_topics import AdminLogTopic
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
    sent = await send_profile_screen(
        message.bot,
        chat_id=message.chat.id,
        caption=join_lines("🎁 " + bold("Промокод"), "", plain("Введите код одним сообщением.")),
        reply_markup=cancel_kb.as_markup(),
        settings=settings,
        delete_message=None,
    )
    await state.update_data(promo_prompt_message_id=sent.message_id)


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
    sent = await answer_callback_with_photo_screen(
        cq,
        caption=join_lines("🎁 " + bold("Промокод"), "", plain("Введите код одним сообщением.")),
        reply_markup=_promo_cancel_keyboard().as_markup(),
        settings=settings,
    )
    if sent is not None:
        await state.update_data(promo_prompt_message_id=sent.message_id)


@router.callback_query(F.data == "promo:cancel")
async def cb_promo_cancel(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cq.answer()
    if cq.message:
        cap = esc("Операция отменена.")
        kb = submenu_back_keyboard()
        if cq.message.photo:
            await cq.message.edit_caption(caption=cap, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await cq.message.edit_text(cap, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)


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

    data = await state.get_data()
    prompt_mid = data.get("promo_prompt_message_id")

    settings = get_settings()
    ok, text, meta = await apply_promo_code_for_user(
        session,
        settings=settings,
        user=db_user,
        raw_code=message.text or "",
    )
    await state.clear()

    await delete_message_safe(message)
    if prompt_mid is not None:
        try:
            await delete_chat_message_safe(message.bot, message.chat.id, int(prompt_mid))
        except (TypeError, ValueError):
            pass

    if ok:
        if meta:
            mt = (meta.get("type") or "").strip().lower()
            lines: list[str]
            if mt == "topup_bonus_percent":
                lines = [
                    f"Код: {code(meta['code'])}",
                    "Тип: " + code(meta["type"]),
                    f"Бонус: +{bold(str(meta['value']))}%",
                    plain("Сработает 1 раз на первое пополнение после активации."),
                ]
            elif mt == "subscription_days":
                fb = meta.get("fallback")
                lines = [
                    f"Код: {code(meta['code'])}",
                    "Тип: " + code(meta["type"]),
                    f"Награда: +{bold(str(meta['value']))} дн. к подписке",
                ]
                if fb is not None:
                    lines.append(f"Фолбэк при отсутствии подписки: +{bold(str(fb))} ₽")
            else:
                lines = [
                    f"Код: {code(meta['code'])}",
                    f"Тип: {code(meta['type'])}",
                    f"Сумма: {bold(str(meta['value']))} ₽",
                ]
            await notify_admin(
                settings,
                title="🎁 " + bold("Промокод применён"),
                lines=lines,
                event_type="promo_apply",
                topic=AdminLogTopic.PROMO,
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
            caption=join_lines(plain("❌ ") + text),
            reply_markup=submenu_back_keyboard(),
            settings=settings,
            delete_message=None,
        )
