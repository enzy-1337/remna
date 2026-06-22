"""Семейная подписка и передача подписки."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.messages import (
    FAMILY_ASK_TARGET,
    FAMILY_ASK_UNBIND,
    FAMILY_BIND_OK_MEMBER,
    FAMILY_BIND_OK_OWNER,
    FAMILY_BTN_BIND,
    FAMILY_BTN_LEAVE,
    FAMILY_BTN_MEMBERS,
    FAMILY_BTN_UNBIND,
    FAMILY_LEAVE_OK,
    FAMILY_MENU_HINT,
    FAMILY_MENU_TITLE,
    FAMILY_NOT_FOUND,
    FAMILY_UNBIND_OK_MEMBER,
    FAMILY_UNBIND_OK_OWNER,
    TRANSFER_ASK_TARGET,
    TRANSFER_CONFIRM_HINT,
    TRANSFER_OK_RECIPIENT,
    TRANSFER_OK_SENDER,
    TRANSFER_ONLY_OWNER,
)
from bot.states.family import FamilyStates, TransferStates
from bot.utils.screen_photo import answer_callback_with_photo_screen, delete_message_safe
from shared.config import Settings, get_settings
from shared.md2 import join_lines, plain, strip_for_popup_alert
from shared.models.user import User
from shared.services.family_service import (
    bind_family_member,
    count_family_members,
    family_bind_allowed,
    get_family_membership,
    list_family_members,
    max_family_extra_members,
    unbind_family_member,
)
from shared.services.subscription_service import get_active_subscription
from shared.services.subscription_transfer_service import transfer_subscription
from shared.services.user_lookup_service import find_user_by_tg_or_username, user_card_label

router = Router(name="family")


def _family_menu_kb(*, is_owner: bool, is_member: bool) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    if is_owner:
        b.row(InlineKeyboardButton(text=FAMILY_BTN_BIND, callback_data="family:bind"))
        b.row(InlineKeyboardButton(text=FAMILY_BTN_MEMBERS, callback_data="family:members"))
        b.row(InlineKeyboardButton(text=FAMILY_BTN_UNBIND, callback_data="family:unbind"))
    if is_member:
        b.row(InlineKeyboardButton(text=FAMILY_BTN_LEAVE, callback_data="family:leave"))
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main", style="danger"))
    return b


def _confirm_kb(prefix: str, target_id: int) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"{prefix}:confirm:{target_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"{prefix}:cancel", style="danger"),
    )
    return b


async def _show_family_menu(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    settings: Settings,
) -> None:
    membership = await get_family_membership(session, user_id=db_user.id)
    is_member = membership is not None
    is_owner = membership is None and await get_active_subscription(session, db_user.id, account_scope=False) is not None
    sub = await get_active_subscription(session, db_user.id, account_scope=False) if is_owner else None
    extra = ""
    if sub is not None:
        limit = max_family_extra_members(int(sub.devices_count))
        used = await count_family_members(session, owner_user_id=db_user.id)
        extra = f"\n\nМожно привязать: {max(0, limit - used)} из {limit}."
    cap = join_lines(
        plain(FAMILY_MENU_TITLE),
        "",
        plain(FAMILY_MENU_HINT + extra),
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=_family_menu_kb(is_owner=is_owner, is_member=is_member).as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "menu:family")
async def cb_family_menu(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    await _show_family_menu(cq, session, db_user, get_settings())


@router.callback_query(F.data == "family:bind")
async def cb_family_bind(cq: CallbackQuery, state: FSMContext, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    await state.set_state(FamilyStates.waiting_bind_target)
    await cq.answer()
    if cq.message:
        await delete_message_safe(cq.message)
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="family:bind:cancel"))
        await cq.message.answer(plain(FAMILY_ASK_TARGET), reply_markup=b.as_markup())


@router.message(FamilyStates.waiting_bind_target, F.text)
async def msg_family_bind_target(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if db_user is None or message.from_user is None:
        return
    target = await find_user_by_tg_or_username(session, message.text or "")
    if target is None:
        await message.answer(plain(FAMILY_NOT_FOUND))
        return
    ok, err = await family_bind_allowed(session, owner=db_user, member=target)
    if not ok:
        await message.answer(plain(err))
        await state.clear()
        return
    await state.update_data(family_target_id=target.id)
    await state.set_state(FamilyStates.waiting_bind_target)
    cap = join_lines(
        plain("Карточка пользователя:"),
        plain(user_card_label(target)),
        "",
        plain("Подтвердить привязку?"),
    )
    await message.answer(cap, reply_markup=_confirm_kb("family:bind", target.id).as_markup())
    await state.set_state(None)


@router.callback_query(F.data.startswith("family:bind:confirm:"))
async def cb_family_bind_confirm(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    try:
        target_id = int((cq.data or "").split(":")[-1])
    except ValueError:
        await cq.answer("Некорректные данные.", show_alert=True)
        return
    target = await session.get(User, target_id)
    if target is None:
        await cq.answer(FAMILY_NOT_FOUND, show_alert=True)
        return
    ok, err, _row = await bind_family_member(session, owner=db_user, member=target)
    if not ok:
        await cq.answer(strip_for_popup_alert(err)[:200], show_alert=True)
        return
    settings = get_settings()
    label = user_card_label(db_user)
    try:
        await cq.bot.send_message(
            int(target.telegram_id),
            plain(FAMILY_BIND_OK_MEMBER.format(label=label)),
        )
    except Exception:
        pass
    await cq.answer("Готово")
    if cq.message:
        await cq.message.edit_text(plain(FAMILY_BIND_OK_OWNER.format(label=user_card_label(target))))


@router.callback_query(F.data == "family:bind:cancel")
async def cb_family_bind_cancel(
    cq: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    await state.clear()
    await cq.answer("Отменено")
    if await reject_if_no_user(cq, db_user):
        return
    assert db_user is not None
    if cq.message:
        await delete_message_safe(cq.message)
    await _show_family_menu(cq, session, db_user, get_settings())


@router.callback_query(F.data == "family:members")
async def cb_family_members(cq: CallbackQuery, session: AsyncSession, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user):
        return
    assert db_user is not None
    rows = await list_family_members(session, owner_user_id=db_user.id)
    if not rows:
        await cq.answer("Участников пока нет.", show_alert=True)
        return
    lines = [plain("👥 Участники семьи:"), ""]
    for i, row in enumerate(rows, 1):
        u = await session.get(User, row.member_user_id)
        if u:
            lines.append(plain(f"{i}. {user_card_label(u)}"))
    await cq.answer()
    if cq.message:
        await cq.message.answer(join_lines(*lines))


@router.callback_query(F.data == "family:unbind")
async def cb_family_unbind(cq: CallbackQuery, state: FSMContext, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user):
        return
    await state.set_state(FamilyStates.waiting_unbind_target)
    await cq.answer()
    if cq.message:
        await delete_message_safe(cq.message)
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="family:unbind:cancel"))
        await cq.message.answer(plain(FAMILY_ASK_UNBIND), reply_markup=b.as_markup())


@router.callback_query(F.data == "family:unbind:cancel")
async def cb_family_unbind_cancel(
    cq: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    await state.clear()
    await cq.answer("Отменено")
    if await reject_if_no_user(cq, db_user):
        return
    assert db_user is not None
    if cq.message:
        await delete_message_safe(cq.message)
    await _show_family_menu(cq, session, db_user, get_settings())


@router.message(FamilyStates.waiting_unbind_target, F.text)
async def msg_family_unbind_target(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if db_user is None:
        return
    target = await find_user_by_tg_or_username(session, message.text or "")
    if target is None:
        await message.answer(plain(FAMILY_NOT_FOUND))
        return
    membership = await get_family_membership(session, user_id=target.id)
    if membership is None or membership.owner_user_id != db_user.id:
        await message.answer(plain("Этот пользователь не в вашей семье."))
        await state.clear()
        return
    settings = get_settings()
    ok, err, _ = await unbind_family_member(
        session, member_user_id=target.id, settings=settings, by_owner=True
    )
    await state.clear()
    if not ok:
        await message.answer(plain(err or "Не удалось отвязать."))
        return
    try:
        await message.bot.send_message(int(target.telegram_id), plain(FAMILY_UNBIND_OK_MEMBER))
    except Exception:
        pass
    await message.answer(plain(FAMILY_UNBIND_OK_OWNER.format(label=user_card_label(target))))


@router.callback_query(F.data == "family:leave")
async def cb_family_leave(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    ok, err, row = await unbind_family_member(
        session, member_user_id=db_user.id, settings=settings, by_owner=False
    )
    if not ok:
        await cq.answer(strip_for_popup_alert(err or "Вы не состоите в семье.")[:200], show_alert=True)
        return
    if row is not None:
        owner = await session.get(User, row.owner_user_id)
        if owner is not None:
            try:
                await cq.bot.send_message(
                    int(owner.telegram_id),
                    plain(f"Участник {user_card_label(db_user)} вышел из семейной подписки."),
                )
            except Exception:
                pass
    await cq.answer("Готово")
    if cq.message:
        await cq.message.answer(plain(FAMILY_LEAVE_OK))


@router.callback_query(F.data == "sub:transfer")
async def cb_transfer_start(
    cq: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    if await get_family_membership(session, user_id=db_user.id) is not None:
        await cq.answer(TRANSFER_ONLY_OWNER, show_alert=True)
        return
    sub = await get_active_subscription(session, db_user.id, account_scope=False)
    if sub is None:
        await cq.answer("Нет активной подписки.", show_alert=True)
        return
    await state.set_state(TransferStates.waiting_recipient)
    await cq.answer()
    if cq.message:
        await delete_message_safe(cq.message)
        _back_kb = InlineKeyboardBuilder()
        _back_kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="sub:transfer:cancel"))
        await cq.message.answer(plain(TRANSFER_ASK_TARGET), reply_markup=_back_kb.as_markup())


@router.callback_query(F.data == "sub:transfer:cancel")
async def cb_transfer_cancel(
    cq: CallbackQuery,
    state: FSMContext,
) -> None:
    await state.clear()
    await cq.answer()
    if cq.message:
        await delete_message_safe(cq.message)
        _b = InlineKeyboardBuilder()
        _b.row(InlineKeyboardButton(text="📋 К подписке", callback_data="menu:sub_main"))
        await cq.message.answer(plain("Передача отменена."), reply_markup=_b.as_markup())


@router.message(TransferStates.waiting_recipient, F.text)
async def msg_transfer_recipient(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if db_user is None:
        return
    from shared.services.family_service import get_family_membership as _gm

    if await _gm(session, user_id=db_user.id) is not None:
        await message.answer(plain(TRANSFER_ONLY_OWNER))
        await state.clear()
        return
    sub = await get_active_subscription(session, db_user.id, account_scope=False)
    if sub is None:
        await message.answer(plain("Нет активной подписки."))
        await state.clear()
        return
    target = await find_user_by_tg_or_username(session, message.text or "")
    if target is None:
        await message.answer(plain(FAMILY_NOT_FOUND))
        return
    if target.id == db_user.id:
        await message.answer(plain("Нельзя передать подписку самому себе."))
        return
    exp = sub.expires_at.strftime("%d.%m.%Y %H:%M UTC")
    cap = plain(
        TRANSFER_CONFIRM_HINT.format(
            label=user_card_label(target),
            expires=exp,
            devices=str(sub.devices_count),
        )
    )
    await state.update_data(transfer_target_id=target.id)
    await message.answer(cap, reply_markup=_confirm_kb("transfer", target.id).as_markup())
    await state.set_state(TransferStates.confirm)


@router.callback_query(F.data.startswith("transfer:confirm:"))
async def cb_transfer_confirm(
    cq: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    try:
        target_id = int((cq.data or "").split(":")[-1])
    except ValueError:
        await cq.answer("Некорректные данные.", show_alert=True)
        return
    target = await session.get(User, target_id)
    if target is None:
        await cq.answer(FAMILY_NOT_FOUND, show_alert=True)
        return
    settings = get_settings()
    ok, msg = await transfer_subscription(session, settings=settings, sender=db_user, recipient=target)
    await state.clear()
    if not ok:
        await cq.answer(strip_for_popup_alert(msg)[:200], show_alert=True)
        return
    try:
        await cq.bot.send_message(
            int(target.telegram_id),
            plain(TRANSFER_OK_RECIPIENT.format(details=msg)),
        )
    except Exception:
        pass
    await cq.answer("Готово")
    if cq.message:
        await cq.message.edit_text(plain(TRANSFER_OK_SENDER.format(label=user_card_label(target))))


@router.callback_query(F.data == "transfer:cancel")
async def cb_transfer_cancel(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cq.answer("Отменено")
