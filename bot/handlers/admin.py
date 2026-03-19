"""Админ-панель: список пользователей, блокировка, поиск по Telegram ID."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.states.admin import AdminFindUserStates
from bot.utils.screen_photo import answer_callback_with_photo_screen, send_profile_screen
from shared.config import get_settings
from shared.md2 import bold, code, esc, italic, join_lines
from shared.models.user import User

logger = logging.getLogger(__name__)

router = Router(name="admin")

PAGE_SIZE = 8


def _is_admin(tg_id: int | None) -> bool:
    if tg_id is None:
        return False
    return tg_id in get_settings().admin_telegram_ids


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📋 Пользователи", callback_data="admin:users:0"))
    b.row(InlineKeyboardButton(text="🔎 Найти по Telegram ID", callback_data="admin:find"))
    b.row(InlineKeyboardButton(text="⬅️ В профиль", callback_data="menu:main"))
    return b.as_markup()


async def _render_user_card(
    cq: CallbackQuery,
    session: AsyncSession,
    *,
    user_id: int,
) -> None:
    settings = get_settings()
    res = await session.execute(select(User).where(User.id == user_id))
    u = res.scalar_one_or_none()
    if u is None:
        await cq.answer("Пользователь не найден", show_alert=True)
        return

    bal = f"{u.balance:.2f}"
    bonus = f"{u.bonus_balance:.2f}"
    reason = esc(u.block_reason or "—")
    full_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or "—"
    lines = [
        "👤 " + bold(f"Пользователь #{u.id}"),
        f"Telegram: {code(str(u.telegram_id))}",
        f"Username: {esc(u.username or '—')}",
        f"Имя: {esc(full_name)}",
        f"Баланс: {bold(bal)} ₽ · бонус: {bold(bonus)} ₽",
        f"Триал использован: {'да' if u.trial_used else 'нет'}",
        f"Статус: {'🚫 заблокирован' if u.is_blocked else '✅ активен'}",
        f"Причина блока: {reason}",
    ]
    b = InlineKeyboardBuilder()
    if u.is_blocked:
        b.row(InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"admin:unblock:{u.id}"))
    else:
        b.row(InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"admin:block:{u.id}"))
    b.row(InlineKeyboardButton(text="⬅️ К списку", callback_data="admin:users:0"))
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(*lines),
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "admin:panel")
async def cb_admin_panel(cq: CallbackQuery, db_user: User | None) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    settings = get_settings()
    text = join_lines(
        "🛠 " + bold("Админ-панель"),
        "",
        "Выберите действие.",
        "",
        italic("Доступ только для ID из ADMIN_TELEGRAM_IDS / ADMIN_TELEGRAM_ID."),
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=text,
        reply_markup=admin_panel_keyboard(),
        settings=settings,
    )


@router.callback_query(F.data.startswith("admin:users:"))
async def cb_admin_users_page(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    try:
        page = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0
    settings = get_settings()

    total = (await session.execute(select(func.count()).select_from(User))).scalar_one()

    offset = page * PAGE_SIZE
    res = await session.execute(
        select(User).order_by(desc(User.id)).offset(offset).limit(PAGE_SIZE)
    )
    rows = list(res.scalars().all())

    lines = [
        "📋 " + bold("Пользователи"),
        f"Стр. {page + 1} · всего записей: {bold(str(total))}",
        "",
    ]
    b = InlineKeyboardBuilder()
    for u in rows:
        un = f"@{esc(u.username)}" if u.username else "—"
        status = "🚫" if u.is_blocked else "✅"
        label = f"{status} #{u.id} tg{u.telegram_id}"
        if len(label) > 64:
            label = label[:61] + "..."
        b.row(InlineKeyboardButton(text=label, callback_data=f"admin:u:{u.id}"))
    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:users:{page - 1}"))
    if offset + len(rows) < total:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:users:{page + 1}"))
    if nav_buttons:
        b.row(*nav_buttons)
    b.row(InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:panel"))

    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(*lines),
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data.startswith("admin:u:"))
async def cb_admin_user_card(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    try:
        uid = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Неверный id", show_alert=True)
        return
    await _render_user_card(cq, session, user_id=uid)


@router.callback_query(F.data.startswith("admin:block:"))
async def cb_admin_block(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    try:
        uid = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Неверный id", show_alert=True)
        return
    res = await session.execute(select(User).where(User.id == uid))
    u = res.scalar_one_or_none()
    if u is None:
        await cq.answer("Не найден", show_alert=True)
        return
    u.is_blocked = True
    u.block_reason = u.block_reason or "Админ-панель"
    await session.commit()
    await _render_user_card(cq, session, user_id=uid)


@router.callback_query(F.data.startswith("admin:unblock:"))
async def cb_admin_unblock(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    try:
        uid = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Неверный id", show_alert=True)
        return
    res = await session.execute(select(User).where(User.id == uid))
    u = res.scalar_one_or_none()
    if u is None:
        await cq.answer("Не найден", show_alert=True)
        return
    u.is_blocked = False
    u.block_reason = None
    await session.commit()
    await _render_user_card(cq, session, user_id=uid)


@router.callback_query(F.data == "admin:find")
async def cb_admin_find_start(
    cq: CallbackQuery,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await state.set_state(AdminFindUserStates.waiting_telegram_id)
    settings = get_settings()
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:find_cancel"))
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "🔎 " + bold("Поиск по Telegram ID"),
            "",
            "Отправьте числом Telegram ID пользователя.",
        ),
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "admin:find_cancel")
async def cb_admin_find_cancel(
    cq: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    await state.clear()
    if cq.from_user is None or not _is_admin(cq.from_user.id):
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await cb_admin_panel(cq, db_user)


@router.message(StateFilter(AdminFindUserStates.waiting_telegram_id), F.text)
async def msg_admin_find_telegram_id(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    db_user: User | None,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        await state.clear()
        return
    if db_user is None:
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(esc("Нужно целое число (Telegram ID)."))
        return
    tg_id = int(raw)
    res = await session.execute(select(User).where(User.telegram_id == tg_id))
    u = res.scalar_one_or_none()
    await state.clear()
    settings = get_settings()
    if u is None:
        await message.answer(join_lines("Не найден пользователь с " + code(str(tg_id)), "", "/admin"))
        return
    un = esc(u.username or "—")
    lines = [
        "🔎 " + bold("Найден"),
        f"#{u.id} · tg {code(str(u.telegram_id))} · @{un}" if u.username else f"#{u.id} · tg {code(str(u.telegram_id))}",
        f"Баланс: {bold(f'{u.balance:.2f}')} ₽",
    ]
    adm = InlineKeyboardBuilder()
    adm.row(InlineKeyboardButton(text="🛠 Карточка", callback_data=f"admin:u:{u.id}"))
    adm.row(InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:panel"))
    await send_profile_screen(
        message.bot,
        chat_id=message.chat.id,
        caption=join_lines(*lines),
        reply_markup=adm.as_markup(),
        settings=settings,
        delete_message=None,
    )


@router.message(F.text == "/admin")
async def cmd_admin(
    message: Message,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    if db_user is None:
        await message.answer(esc("Сначала /start"))
        return
    settings = get_settings()
    text = join_lines(
        "🛠 " + bold("Админ-панель"),
        "",
        "Выберите действие или используйте кнопку в профиле.",
        "",
        italic("Доступ только для ID из ADMIN_TELEGRAM_IDS / ADMIN_TELEGRAM_ID."),
    )
    await send_profile_screen(
        message.bot,
        chat_id=message.chat.id,
        caption=text,
        reply_markup=admin_panel_keyboard(),
        settings=settings,
        delete_message=None,
    )
