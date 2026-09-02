"""Вкладка "Логи" — просмотр/поиск прямо из бота, без захода на сервер.

Два раздела:
- "Активность" — notifications_log: практически все заметные события (регистрации,
  оплаты, промокоды, устройства, антифрод, массовые выдачи, рассылки, бэкапы,
  запуск бота/API и т.д.) — всё, что проходит через notify_admin*() в
  shared/services/admin_notify.py, а не только ошибки.
- "Ошибки" — app_error_logs: сырые ERROR+ трейсбеки (см. shared/services/admin_error_log_handler.py,
  тот же handler пишет их сюда).
"""

from __future__ import annotations

from typing import Literal
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
from shared.models.notification_log import NotificationLog
from shared.models.user import User

router = Router(name="logs_viewer")

_MSK_TZ = ZoneInfo("Europe/Moscow")
_LIST_LIMIT = 8
_MSG_TRUNC = 160
_TEXT_MSG_MAX = 3800

Tab = Literal["activity", "errors"]
_DEFAULT_TAB: Tab = "activity"


def _keyboard(tab: Tab) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=("✅ " if tab == "activity" else "") + "📋 Активность",
            callback_data="admin:logs:tab:activity",
        ),
        InlineKeyboardButton(
            text=("✅ " if tab == "errors" else "") + "🛑 Ошибки",
            callback_data="admin:logs:tab:errors",
        ),
    )
    b.row(
        InlineKeyboardButton(text="🔎 Поиск по тексту", callback_data=f"admin:logs:search:{tab}"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin:logs:tab:{tab}"),
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:section:analytics"))
    return b.as_markup()


def _fmt_ts(dt) -> str:
    return dt.astimezone(_MSK_TZ).strftime("%d.%m %H:%M") if dt else "—"


def _fmt_error_entry(row: AppErrorLog) -> str:
    msg = (row.message or "").strip().replace("\n", " ")
    if len(msg) > _MSG_TRUNC:
        msg = msg[:_MSG_TRUNC] + "…"
    return join_lines(
        plain(f"🕐 {_fmt_ts(row.created_at)} · {row.service} · ") + code(row.logger_name),
        code(msg) if msg else plain("(пусто)"),
    )


def _fmt_activity_entry(row: NotificationLog) -> str:
    first_line = (row.message_text or "").strip().splitlines()[0] if row.message_text else ""
    if len(first_line) > _MSG_TRUNC:
        first_line = first_line[:_MSG_TRUNC] + "…"
    status_icon = {"sent": "✅", "failed": "⚠️", "skipped_no_admin_chat": "➖"}.get(row.status, "•")
    who = f"user#{row.user_id}" if row.user_id is not None else "система"
    return join_lines(
        plain(f"🕐 {_fmt_ts(row.sent_at)} · ") + code(row.type) + plain(f" · {who} {status_icon}"),
        code(first_line) if first_line else plain("(пусто)"),
    )


def _render_rows(rows: list, *, title: str, tab: Tab) -> str:
    if not rows:
        return join_lines("📄 " + bold(title), "", plain("Ничего не найдено."))
    parts = ["📄 " + bold(title), ""]
    fmt = _fmt_error_entry if tab == "errors" else _fmt_activity_entry
    for row in rows:
        parts.append(fmt(row))
        parts.append("")
    return join_lines(*parts).rstrip()


async def _load_errors(session: AsyncSession, *, query: str | None = None) -> list[AppErrorLog]:
    stmt = select(AppErrorLog)
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            or_(
                AppErrorLog.message.ilike(like),
                AppErrorLog.traceback.ilike(like),
                AppErrorLog.logger_name.ilike(like),
                AppErrorLog.service.ilike(like),
            )
        )
    stmt = stmt.order_by(desc(AppErrorLog.created_at)).limit(_LIST_LIMIT)
    return list((await session.execute(stmt)).scalars())


async def _load_activity(session: AsyncSession, *, query: str | None = None) -> list[NotificationLog]:
    stmt = select(NotificationLog)
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            or_(NotificationLog.message_text.ilike(like), NotificationLog.type.ilike(like))
        )
    stmt = stmt.order_by(desc(NotificationLog.sent_at)).limit(_LIST_LIMIT)
    return list((await session.execute(stmt)).scalars())


async def _show_tab(cq: CallbackQuery, session: AsyncSession, tab: Tab) -> None:
    rows = await _load_errors(session) if tab == "errors" else await _load_activity(session)
    title = f"Последние {len(rows)} ошибок" if tab == "errors" else f"Последние {len(rows)} событий"
    caption = _render_rows(rows, title=title, tab=tab)
    await answer_callback_with_photo_screen(
        cq,
        caption=caption,
        reply_markup=_keyboard(tab),
        settings=get_settings(),
        photo_key="admin:logs",
    )


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
    await cq.answer()
    await _show_tab(cq, session, _DEFAULT_TAB)


@router.callback_query(F.data.startswith("admin:logs:tab:"))
async def cb_logs_tab(
    cq: CallbackQuery, state: FSMContext, session: AsyncSession, is_bot_admin: bool = False
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    tab = (cq.data or "").rsplit(":", 1)[-1]
    if tab not in ("activity", "errors"):
        tab = _DEFAULT_TAB
    await state.clear()
    await cq.answer()
    await _show_tab(cq, session, tab)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith("admin:logs:search:"))
async def cb_logs_search_start(
    cq: CallbackQuery, state: FSMContext, is_bot_admin: bool = False
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    tab = (cq.data or "").rsplit(":", 1)[-1]
    if tab not in ("activity", "errors"):
        tab = _DEFAULT_TAB
    await state.update_data(logs_tab=tab)
    await state.set_state(AdminLogsStates.waiting_query)
    await cq.answer()
    if cq.message is None:
        return
    hint = (
        "Пришлите ключевое слово для поиска по активности (тип события/текст)."
        if tab == "activity"
        else "Пришлите ключевое слово для поиска по логам (сообщение/трейсбек/логгер)."
    )
    await cq.message.answer(plain(hint))


@router.message(StateFilter(AdminLogsStates.waiting_query), F.text)
async def msg_logs_search_query(
    message: Message, state: FSMContext, session: AsyncSession, is_bot_admin: bool = False
) -> None:
    if message.from_user is None or not is_bot_admin:
        await state.clear()
        return
    data = await state.get_data()
    tab: Tab = data.get("logs_tab", _DEFAULT_TAB)
    query = (message.text or "").strip()
    await state.clear()
    if not query:
        await message.answer(plain("Пустой запрос."))
        return

    rows = await _load_errors(session, query=query) if tab == "errors" else await _load_activity(
        session, query=query
    )
    caption = truncate_caption(_render_rows(rows, title=f"Найдено по «{query}»", tab=tab), _TEXT_MSG_MAX)
    try:
        await message.answer(caption, reply_markup=_keyboard(tab))
    except TelegramBadRequest as e:
        if "can't parse entities" not in str(e):
            raise
        await message.answer(
            strip_for_popup_alert(caption), reply_markup=_keyboard(tab), parse_mode=None
        )
