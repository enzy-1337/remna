"""Массовая выдача баланса или дней подписки: всем пользователям или по списку
Telegram ID, с опциональным фильтром "была подписка за последние N дней" —
не подошедшие пропускаются, а не считаются ошибкой. См. shared/services/mass_grant_service.py.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.states.admin import AdminMassGrantStates
from bot.utils.screen_photo import answer_callback_with_photo_screen
from shared.config import get_settings
from shared.md2 import bold, join_lines, plain
from shared.models.user import User
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin
from shared.services.mass_grant_service import apply_mass_grant, resolve_candidate_users

router = Router(name="mass_grant")

_CANCEL_ROW = InlineKeyboardButton(text="❌ Отмена", callback_data="admin:mass_grant:cancel")


def _type_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="💰 Баланс", callback_data="admin:mass_grant:type:balance"),
        InlineKeyboardButton(text="📅 Дни подписки", callback_data="admin:mass_grant:type:days"),
    )
    b.row(_CANCEL_ROW)
    return b.as_markup()


def _cancel_only_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_CANCEL_ROW)
    return b.as_markup()


def _filter_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="Без фильтра", callback_data="admin:mass_grant:filter:skip"),
        InlineKeyboardButton(text="Указать N дней", callback_data="admin:mass_grant:filter:ask"),
    )
    b.row(_CANCEL_ROW)
    return b.as_markup()


def _confirm_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="admin:mass_grant:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin:mass_grant:cancel"),
    )
    return b.as_markup()


@router.callback_query(F.data == "admin:mass_grant")
async def cb_mass_grant_start(
    cq: CallbackQuery, state: FSMContext, db_user: User | None, is_bot_admin: bool = False
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if db_user is None:
        await cq.answer("Сначала /start", show_alert=True)
        return
    await state.clear()
    await cq.answer()
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "🎁 " + bold("Массовая выдача"),
            "",
            plain("Что выдать пользователям?"),
        ),
        reply_markup=_type_keyboard(),
        settings=get_settings(),
        photo_key="admin:mass_grant",
    )


@router.callback_query(F.data.startswith("admin:mass_grant:type:"))
async def cb_mass_grant_type(
    cq: CallbackQuery, state: FSMContext, db_user: User | None, is_bot_admin: bool = False
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    grant_type = (cq.data or "").rsplit(":", 1)[-1]
    if grant_type not in ("balance", "days"):
        await cq.answer("Некорректный тип.", show_alert=True)
        return
    await state.update_data(mg_grant_type=grant_type)
    await state.set_state(AdminMassGrantStates.waiting_audience)
    await cq.answer()
    if cq.message is None:
        return
    await cq.message.answer(
        join_lines(
            plain("Кому выдать?"),
            "",
            plain("Отправьте ") + bold("все") + plain(" — выдать всем пользователям,"),
            plain("или список Telegram ID (по одному на строку / через запятую)."),
        ),
        reply_markup=_cancel_only_keyboard(),
    )


@router.message(StateFilter(AdminMassGrantStates.waiting_audience), F.text)
async def msg_mass_grant_audience(
    message: Message, state: FSMContext, is_bot_admin: bool = False
) -> None:
    if message.from_user is None or not is_bot_admin:
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw.lower() in ("все", "всем", "all"):
        await state.update_data(mg_audience_ids=None)
    else:
        ids: set[int] = set()
        for chunk in raw.replace(",", "\n").splitlines():
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                ids.add(int(chunk))
            except ValueError:
                await message.answer(
                    plain(f"Не число: «{chunk}». Пришлите список Telegram ID или «все».")
                )
                return
        if not ids:
            await message.answer(plain("Список пуст. Пришлите Telegram ID или «все»."))
            return
        await state.update_data(mg_audience_ids=sorted(ids))

    await state.set_state(AdminMassGrantStates.waiting_filter_days)
    await message.answer(
        join_lines(
            plain("Применить фильтр «была подписка за последние N дней»?"),
            plain("Не подошедшие под условие пользователи будут пропущены."),
        ),
        reply_markup=_filter_keyboard(),
    )


@router.callback_query(F.data == "admin:mass_grant:filter:skip", StateFilter(AdminMassGrantStates.waiting_filter_days))
async def cb_mass_grant_filter_skip(
    cq: CallbackQuery, state: FSMContext, is_bot_admin: bool = False
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    await state.update_data(mg_filter_days=None)
    await cq.answer()
    if cq.message is None:
        return
    await _ask_amount(cq.message, state)


@router.callback_query(F.data == "admin:mass_grant:filter:ask", StateFilter(AdminMassGrantStates.waiting_filter_days))
async def cb_mass_grant_filter_ask(
    cq: CallbackQuery, state: FSMContext, is_bot_admin: bool = False
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    await cq.answer()
    if cq.message is None:
        return
    await cq.message.answer(
        plain("Введите N — количество дней (например, 7)."),
        reply_markup=_cancel_only_keyboard(),
    )


@router.message(StateFilter(AdminMassGrantStates.waiting_filter_days), F.text)
async def msg_mass_grant_filter_days(
    message: Message, state: FSMContext, is_bot_admin: bool = False
) -> None:
    if message.from_user is None or not is_bot_admin:
        await state.clear()
        return
    raw = (message.text or "").strip()
    try:
        n = int(raw)
    except ValueError:
        await message.answer(plain("Нужно целое число дней, например 7."))
        return
    if n <= 0:
        await message.answer(plain("Число дней должно быть больше нуля."))
        return
    await state.update_data(mg_filter_days=n)
    await _ask_amount(message, state)


async def _ask_amount(target, state: FSMContext) -> None:
    data = await state.get_data()
    grant_type = data.get("mg_grant_type")
    await state.set_state(AdminMassGrantStates.waiting_amount)
    prompt = (
        plain("Введите сумму в ₽ (например, 100 или 100.5).")
        if grant_type == "balance"
        else plain("Введите количество дней (например, 7).")
    )
    await target.answer(prompt, reply_markup=_cancel_only_keyboard())


@router.message(StateFilter(AdminMassGrantStates.waiting_amount), F.text)
async def msg_mass_grant_amount(
    message: Message, state: FSMContext, is_bot_admin: bool = False
) -> None:
    if message.from_user is None or not is_bot_admin:
        await state.clear()
        return
    data = await state.get_data()
    grant_type = data.get("mg_grant_type")
    raw = (message.text or "").strip().replace(",", ".")

    if grant_type == "balance":
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            await message.answer(plain("Нужно число, например 100 или 100.5."))
            return
        if amount <= 0:
            await message.answer(plain("Сумма должна быть > 0."))
            return
        await state.update_data(mg_amount_rub=str(amount), mg_days=None)
        amount_line = plain("Сумма: ") + bold(f"+{amount} ₽")
    else:
        try:
            days = int(raw)
        except ValueError:
            await message.answer(plain("Нужно целое число дней, например 7."))
            return
        if days <= 0:
            await message.answer(plain("Количество дней должно быть > 0."))
            return
        await state.update_data(mg_amount_rub=None, mg_days=days)
        amount_line = plain("Дни: ") + bold(f"+{days} дн.")

    audience_ids = data.get("mg_audience_ids")
    filter_days = data.get("mg_filter_days")
    audience_line = (
        plain("Всем пользователям") if audience_ids is None else plain(f"По списку: {len(audience_ids)} ID")
    )
    filter_line = (
        plain("Без фильтра") if filter_days is None else plain(f"Только с подпиской за последние {filter_days} дн.")
    )

    await state.set_state(AdminMassGrantStates.waiting_confirm)
    await message.answer(
        join_lines(
            "🎁 " + bold("Проверьте параметры"),
            "",
            amount_line,
            plain("Кому: ") + audience_line,
            plain("Фильтр: ") + filter_line,
            "",
            plain("Подтвердить запуск?"),
        ),
        reply_markup=_confirm_keyboard(),
    )


@router.callback_query(F.data == "admin:mass_grant:confirm", StateFilter(AdminMassGrantStates.waiting_confirm))
async def cb_mass_grant_confirm(
    cq: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()

    grant_type = data.get("mg_grant_type")
    audience_ids = data.get("mg_audience_ids")
    filter_days = data.get("mg_filter_days")
    amount_raw = data.get("mg_amount_rub")
    days = data.get("mg_days")
    amount = Decimal(amount_raw) if amount_raw is not None else None

    if grant_type not in ("balance", "days") or (grant_type == "balance" and amount is None) or (
        grant_type == "days" and days is None
    ):
        await cq.answer("Данные устарели, начните заново.", show_alert=True)
        return

    await cq.answer("Выполняю…")
    settings = get_settings()

    actor_label = f"tg:{cq.from_user.id}"
    users = await resolve_candidate_users(session, telegram_ids=audience_ids)
    result = await apply_mass_grant(
        session,
        settings,
        users=users,
        grant_type=grant_type,
        amount_rub=amount,
        days=days,
        subscription_within_days=filter_days,
        actor_label=actor_label,
    )
    await session.commit()

    what = f"+{amount} ₽" if grant_type == "balance" else f"+{days} дн."
    summary = join_lines(
        "✅ " + bold("Массовая выдача завершена"),
        "",
        plain(f"Что: {what}"),
        plain(f"Кандидатов: {result.total_candidates}"),
        plain(f"Выдано: {result.granted}"),
        plain(f"Пропущено (нет подписки за окно): {result.skipped_no_subscription}"),
        plain(f"Пропущено (некорректный юзер): {result.skipped_invalid_user}"),
        plain(f"Ошибок: {len(result.errors)}"),
    )
    if cq.message is not None:
        await cq.message.answer(summary)

    if db_user is not None:
        await notify_admin(
            settings,
            title="🎁 " + bold("Массовая выдача"),
            lines=[
                plain(f"Что: {what}"),
                plain(
                    f"Кандидатов: {result.total_candidates} · выдано: {result.granted} · "
                    f"пропущено (нет подписки): {result.skipped_no_subscription} · "
                    f"ошибок: {len(result.errors)}"
                ),
                plain(f"Кто запустил: {actor_label}"),
            ],
            event_type="mass_grant_bot",
            topic=AdminLogTopic.BONUSES,
            subject_user=db_user,
            session=session,
        )


@router.callback_query(F.data == "admin:mass_grant:cancel")
async def cb_mass_grant_cancel(
    cq: CallbackQuery, state: FSMContext, db_user: User | None, is_bot_admin: bool = False
) -> None:
    await state.clear()
    if cq.from_user is None or not is_bot_admin:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    await cq.answer("Отменено.")
    if cq.message is not None:
        try:
            await cq.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    from bot.handlers.admin import cb_admin_section_analytics

    if db_user is not None:
        await cb_admin_section_analytics(cq, db_user, is_bot_admin=is_bot_admin)
