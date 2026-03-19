"""Баланс: история, пополнение CryptoBot / Platega."""

from __future__ import annotations

import html
import logging
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.keyboards.inline import topup_amounts_keyboard, topup_invoice_done_keyboard, topup_providers_keyboard
from bot.states.payment import TopupStates
from bot.utils.screen_photo import answer_callback_with_photo_screen
from shared.config import get_settings
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.topup_service import create_topup_payment

logger = logging.getLogger(__name__)

router = Router(name="balance")


def _can_cryptobot() -> bool:
    s = get_settings()
    return s.cryptobot_stub or bool(s.cryptobot_token.strip())


def _can_platega() -> bool:
    s = get_settings()
    return s.platega_stub or bool(s.platega_merchant_id.strip() and s.platega_secret_key.strip())


async def _history_lines(session: AsyncSession, user_id: int, limit: int = 6) -> list[str]:
    r = await session.execute(
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(Transaction.id.desc())
        .limit(limit)
    )
    rows = r.scalars().all()
    if not rows:
        return ["История пуста."]
    lines: list[str] = []
    for t in rows:
        st = html.escape(t.status)
        amt = html.escape(str(t.amount))
        cur = html.escape(t.currency or "RUB")
        ptype = html.escape(t.type)
        prov = html.escape(t.payment_provider or "—")
        lines.append(f"• {ptype} | {amt} {cur} | {st} | {prov}")
    return lines


def _balance_caption(user: User, history: list[str]) -> str:
    bal = html.escape(str(user.balance))
    bonus = html.escape(str(user.bonus_balance))
    hist_block = "\n".join(history)
    return (
        f"💰 <b>Баланс</b>\n\n"
        f"Основной: <b>{bal}</b> ₽\n"
        f"Бонусный: <b>{bonus}</b> ₽\n\n"
        f"<b>Последние операции:</b>\n{hist_block}"
    )


async def _edit_or_send_balance(
    cq: CallbackQuery,
    *,
    caption: str,
    reply_markup,
) -> None:
    if cq.message is None:
        return
    if cq.message.photo:
        await cq.message.edit_caption(caption=caption, reply_markup=reply_markup)
    else:
        await cq.message.edit_text(caption, reply_markup=reply_markup)


@router.callback_query(F.data == "menu:balance")
async def cb_balance_home(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    hist = await _history_lines(session, db_user.id)
    cap = _balance_caption(db_user, hist)
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=topup_amounts_keyboard(),
        settings=settings,
    )


@router.callback_query(F.data == "topup:back_amt")
async def cb_topup_back_amt(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    hist = await _history_lines(session, db_user.id)
    await cq.answer()
    settings = get_settings()
    if cq.message and cq.message.photo:
        await cq.message.edit_caption(
            caption=_balance_caption(db_user, hist),
            reply_markup=topup_amounts_keyboard(),
        )
    else:
        await answer_callback_with_photo_screen(
            cq,
            caption=_balance_caption(db_user, hist),
            reply_markup=topup_amounts_keyboard(),
            settings=settings,
        )


@router.callback_query(F.data.startswith("topup:amt:"))
async def cb_topup_amount(
    cq: CallbackQuery,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    try:
        amt = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Некорректная сумма", show_alert=True)
        return
    if amt <= 0:
        await cq.answer("Некорректная сумма", show_alert=True)
        return
    await cq.answer()
    text = f"Пополнение на <b>{amt}</b> ₽\n\nВыберите способ оплаты:"
    await _edit_or_send_balance(cq, caption=text, reply_markup=topup_providers_keyboard(amt))


@router.callback_query(F.data == "topup:custom")
async def cb_topup_custom(
    cq: CallbackQuery,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    await state.set_state(TopupStates.waiting_amount_rub)
    await cq.answer()
    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="topup:cancel_fsm"))
    await _edit_or_send_balance(
        cq,
        caption="Введите сумму в рублях (целое число), от <b>50</b> до <b>100000</b>:",
        reply_markup=cancel_kb.as_markup(),
    )


@router.callback_query(F.data == "topup:cancel_fsm")
async def cb_topup_cancel_fsm(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
) -> None:
    await state.clear()
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    hist = await _history_lines(session, db_user.id)
    await cq.answer()
    settings = get_settings()
    if cq.message and cq.message.photo:
        await cq.message.edit_caption(
            caption=_balance_caption(db_user, hist),
            reply_markup=topup_amounts_keyboard(),
        )
    else:
        await answer_callback_with_photo_screen(
            cq,
            caption=_balance_caption(db_user, hist),
            reply_markup=topup_amounts_keyboard(),
            settings=settings,
        )


@router.message(TopupStates.waiting_amount_rub, F.text)
async def msg_topup_custom_amount(
    message: Message,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await reject_if_blocked(message, db_user) or db_user is None:
        await state.clear()
        return
    assert db_user is not None
    raw = (message.text or "").strip().replace(",", ".")
    try:
        d = Decimal(raw)
    except InvalidOperation:
        await message.answer("Введите число, например <code>250</code>")
        return
    if d != d.to_integral_value():
        await message.answer("Укажите целое число рублей.")
        return
    amt = int(d)
    if amt < 50 or amt > 100_000:
        await message.answer("Допустимо от 50 до 100000 ₽.")
        return
    await state.clear()
    await message.answer(
        f"Пополнение на <b>{amt}</b> ₽\n\nВыберите способ оплаты:",
        reply_markup=topup_providers_keyboard(amt),
    )


@router.callback_query(F.data.startswith("topup:prov:"))
async def cb_topup_provider(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    tg_user,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    if len(parts) != 4 or parts[0] != "topup" or parts[1] != "prov":
        await cq.answer("Ошибка данных", show_alert=True)
        return
    prov_name, amount_s = parts[2], parts[3]
    try:
        amount_rub = Decimal(amount_s)
    except InvalidOperation:
        await cq.answer("Ошибка суммы", show_alert=True)
        return

    if prov_name == "cryptobot" and not _can_cryptobot():
        await cq.answer("CryptoBot не настроен (CRYPTOBOT_TOKEN)", show_alert=True)
        return
    if prov_name == "platega" and not _can_platega():
        await cq.answer("Platega не настроена (MERCHANT_ID / SECRET)", show_alert=True)
        return

    settings = get_settings()
    tg = tg_user or cq.from_user
    if tg is None:
        await cq.answer("Нет пользователя Telegram", show_alert=True)
        return

    try:
        _txn, pay_url = await create_topup_payment(
            session,
            user=db_user,
            telegram_id=tg.id,
            amount_rub=amount_rub,
            provider_name=prov_name,
            settings=settings,
        )
    except Exception:
        logger.exception("create_topup_payment failed")
        await cq.answer("Не удалось создать платёж. Попробуйте позже.", show_alert=True)
        return

    await cq.answer()
    href = html.escape(pay_url, quote=True)
    label = "CryptoBot" if prov_name == "cryptobot" else "Platega"
    text = (
        f"💳 Счёт через <b>{label}</b> на <b>{amount_s}</b> ₽ создан.\n\n"
        f'<a href="{href}">Открыть страницу оплаты</a>\n\n'
        "После оплаты баланс обновится автоматически (обычно в течение минуты)."
    )
    if cq.message:
        if cq.message.photo:
            await cq.message.edit_caption(caption=text, reply_markup=topup_invoice_done_keyboard())
        else:
            await cq.message.edit_text(text, reply_markup=topup_invoice_done_keyboard())
