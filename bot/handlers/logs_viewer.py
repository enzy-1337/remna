"""Просмотр/поиск по app_error_logs прямо из бота — без захода на сервер.
См. shared/services/admin_error_log_handler.py (тот же handler пишет ERROR+ логи сюда)."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.states.admin import AdminLogsStates
from bot.utils.screen_photo import answer_callback_with_photo_screen, truncate_caption
from shared.config import get_settings
from shared.md2 import bold, code, join_lines, plain, strip_for_popup_alert
from shared.models.app_error_log import AppErrorLog
from shared.models.user import User

router = Router(name="logs_viewer")

_MSK_TZ = ZoneInfo("Europe/Moscow")
_LIST_LIMIT = 8
_MSG_TRUNC = 200
_TEXT_MSG_MAX = 3800


def _keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🔎 Поиск по тексту", callback_data="admin:logs:search"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:logs"),
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:section:analytics"))
    return b.as_markup()


def _fmt_entry(row: AppErrorLog) -> str:
    ts = row.created_at.astimezone(_MSK_TZ).strftime("%d.%m %H:%M") if row.created_at else "—"
    msg = (row.message or "").strip().replace("\n", " ")
    if len(msg) > _MSG_TRUNC:
        msg = msg[:_MSG_TRUNC] + "…"
    return join_lines(
        plain(f"🕐 {ts} · {row.service} · ") + code(row.logger_name),
        code(msg) if msg else plain("(пусто)"),
    )


def _render_rows(rows: list[AppErrorLog], *, title: str) -> str:
    if not rows:
        return join_lines("📄 " + bold(title), "", plain("Ничего не найдено."))
    parts = ["📄 " + bold(title), ""]
    for row in rows:
        parts.append(_fmt_entry(row))
        parts.append("")
    return join_lines(*parts).rstrip()


@router.callback_query(F.data == "admin:logs")
async def cb_logs_start(
    cq: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User | None, is_bot_admin: bool = False
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await state.clear()
    rows = list(
        (
            await session.execute(
                select(AppErrorLog).order_by(desc(AppErrorLog.created_at)).limit(_LIST_LIMIT)
            )
        ).scalars()
    )
    caption = _render_rows(rows, title=f"Последние {len(rows)} ошибок")
    await cq.answer()
    await answer_callback_with_photo_screen(
        cq,
        caption=caption,
        reply_markup=_keyboard(),
        settings=get_settings(),
        photo_key="admin:logs",
    )


@router.callback_query(F.data == "admin:logs:search")
async def cb_logs_search_start(
    cq: CallbackQuery, state: FSMContext, is_bot_admin: bool = False
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminLogsStates.waiting_query)
    await cq.answer()
    if cq.message is None:
        return
    await cq.message.answer(
        plain("Пришлите ключевое слово для поиска по логам (сообщение/трейсбек/логгер)."),
    )


@router.message(StateFilter(AdminLogsStates.waiting_query), F.text)
async def msg_logs_search_query(
    message: Message, state: FSMContext, session: AsyncSession, is_bot_admin: bool = False
) -> None:
    if message.from_user is None or not is_bot_admin:
        await state.clear()
        return
    query = (message.text or "").strip()
    await state.clear()
    if not query:
        await message.answer(plain("Пустой запрос."))
        return

    like = f"%{query}%"
    rows = list(
        (
            await session.execute(
                select(AppErrorLog)
                .where(
                    or_(
                        AppErrorLog.message.ilike(like),
                        AppErrorLog.traceback.ilike(like),
                        AppErrorLog.logger_name.ilike(like),
                        AppErrorLog.service.ilike(like),
                    )
                )
                .order_by(desc(AppErrorLog.created_at))
                .limit(_LIST_LIMIT)
            )
        ).scalars()
    )
    caption = truncate_caption(_render_rows(rows, title=f"Найдено по «{query}»"), _TEXT_MSG_MAX)
    try:
        await message.answer(caption, reply_markup=_keyboard())
    except TelegramBadRequest as e:
        if "can't parse entities" not in str(e):
            raise
        await message.answer(
            strip_for_popup_alert(caption), reply_markup=_keyboard(), parse_mode=None
        )
