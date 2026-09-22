"""Привязка почты в Telegram-боте — тот же email потом работает для входа на сайт
(общее поле User.email/email_verified_at, см. shared/services/email_code_service.py)."""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.keyboards.profile_kb import profile_main_keyboard
from bot.states.email_link import EmailLinkStates
from bot.ui.profile_text import profile_caption
from bot.utils.screen_photo import answer_callback_with_photo_screen, delete_message_safe
from shared.config import get_settings
from shared.md2 import bold, code, join_lines, plain
from shared.models.user import User
from shared.services.email_code_service import (
    email_sending_configured,
    normalize_email,
    start_email_code,
    verify_email_code,
)
from shared.services.subscription_service import get_active_subscription
from shared.services.trial_service import trial_eligible

router = Router(name="email_link")

_LINK_PURPOSE = "bot_link"


def _email_link_keyboard(*, has_linked: bool, show_resend: bool = False) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    if show_resend:
        b.row(InlineKeyboardButton(text="✉️ Отправить код ещё раз", callback_data="email:resend"))
    if has_linked:
        b.row(InlineKeyboardButton(text="🗑 Отвязать почту", callback_data="email:unlink"))
    b.row(InlineKeyboardButton(text="⬅️ В профиль", callback_data="email:cancel"))
    return b


async def _back_to_profile(cq: CallbackQuery, session: AsyncSession, db_user: User, is_bot_admin: bool) -> None:
    settings = get_settings()
    tg = cq.from_user
    if tg is None:
        await cq.answer()
        return
    has_act = await get_active_subscription(session, db_user.id) is not None
    show_trial = bool(settings.trial_enabled and trial_eligible(db_user, has_act))
    cap = profile_caption(db_user, tg, is_admin=is_bot_admin)
    kb = profile_main_keyboard(
        show_trial=show_trial,
        support_url=None,
        is_admin=is_bot_admin,
    )
    await cq.answer()
    await answer_callback_with_photo_screen(cq, caption=cap, reply_markup=kb, settings=settings, photo_key="menu:main")


@router.callback_query(F.data == "menu:email")
async def cb_email_open(cq: CallbackQuery, db_user: User | None, state: FSMContext) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    if not email_sending_configured(settings):
        await cq.answer("Привязка почты пока не настроена.", show_alert=True)
        return
    await state.set_state(EmailLinkStates.waiting_email)
    current = db_user.email if db_user.email_verified_at else None
    status = f"привязана: {current}" if current else "не привязана"
    caption = join_lines(
        "✉️ " + bold("Привязка почты"),
        "",
        plain("Сейчас: ") + bold(status),
        "",
        plain("Эта же почта будет работать для входа на сайт. Отправьте адрес почты сообщением."),
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=caption,
        reply_markup=_email_link_keyboard(has_linked=bool(current)).as_markup(),
        settings=settings,
        photo_key="admin:section:profile",
    )


@router.callback_query(F.data == "email:cancel")
async def cb_email_cancel(cq: CallbackQuery, session: AsyncSession, db_user: User | None, state: FSMContext, is_bot_admin: bool = False) -> None:
    await state.clear()
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    await _back_to_profile(cq, session, db_user, is_bot_admin)


@router.callback_query(F.data == "email:unlink")
async def cb_email_unlink(cq: CallbackQuery, db_user: User | None, state: FSMContext) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    db_user.email = None
    db_user.email_verified_at = None
    await state.set_state(EmailLinkStates.waiting_email)
    settings = get_settings()
    await cq.answer("Почта отвязана")
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "✉️ " + bold("Привязка почты"),
            "",
            plain("Связь удалена. Отправьте новый адрес почты или нажмите «В профиль»."),
        ),
        reply_markup=_email_link_keyboard(has_linked=False).as_markup(),
        settings=settings,
        photo_key="admin:section:profile",
    )


@router.callback_query(F.data == "email:resend")
async def cb_email_resend(cq: CallbackQuery, db_user: User | None, state: FSMContext) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    data = await state.get_data()
    pending = data.get("pending_email")
    if not pending:
        await cq.answer("Сначала отправьте адрес почты.", show_alert=True)
        return
    ok, err = await start_email_code(pending, purpose=_LINK_PURPOSE)
    await cq.answer(("Код отправлен повторно" if ok else err), show_alert=not ok)


@router.message(EmailLinkStates.waiting_email, F.text)
async def msg_email_address(message: Message, session: AsyncSession, db_user: User | None, state: FSMContext) -> None:
    if await reject_if_blocked(message, db_user) or db_user is None:
        await state.clear()
        return
    email = normalize_email(message.text or "")
    if email is None:
        await message.answer(join_lines("❌ " + bold("Неверный формат почты"), plain("Пример: ") + code("name@example.com")))
        return
    taken = (
        await session.execute(
            select(User.id).where(User.id != db_user.id, User.email == email, User.email_verified_at.is_not(None)).limit(1)
        )
    ).scalar_one_or_none()
    if taken is not None:
        await message.answer(plain("Эта почта уже привязана к другому аккаунту."))
        return
    ok, err = await start_email_code(email, purpose=_LINK_PURPOSE)
    if not ok:
        await message.answer(plain(err))
        return
    await state.update_data(pending_email=email)
    await state.set_state(EmailLinkStates.waiting_code)
    await delete_message_safe(message)
    await message.answer(
        join_lines(
            "✉️ " + bold("Код отправлен"),
            plain("Отправили 6-значный код на ") + bold(email) + plain(" — введите его сюда."),
        ),
        reply_markup=_email_link_keyboard(has_linked=False, show_resend=True).as_markup(),
    )


@router.message(EmailLinkStates.waiting_code, F.text)
async def msg_email_code(message: Message, session: AsyncSession, db_user: User | None, state: FSMContext) -> None:
    if await reject_if_blocked(message, db_user) or db_user is None:
        await state.clear()
        return
    data = await state.get_data()
    pending = data.get("pending_email")
    if not pending:
        await state.set_state(EmailLinkStates.waiting_email)
        await message.answer(plain("Сессия истекла, отправьте адрес почты заново."))
        return
    ok, err = await verify_email_code(pending, message.text or "", purpose=_LINK_PURPOSE)
    if not ok:
        await message.answer(plain(err))
        return
    taken = (
        await session.execute(
            select(User.id).where(User.id != db_user.id, User.email == pending, User.email_verified_at.is_not(None)).limit(1)
        )
    ).scalar_one_or_none()
    if taken is not None:
        await state.clear()
        await message.answer(plain("Эта почта уже привязана к другому аккаунту."))
        return
    db_user.email = pending
    db_user.email_verified_at = datetime.now(timezone.utc)
    await state.clear()
    await delete_message_safe(message)
    await message.answer(
        join_lines(
            "✅ " + bold("Почта привязана"),
            plain("Теперь ей можно входить на сайт: ") + code(db_user.email),
        )
    )
