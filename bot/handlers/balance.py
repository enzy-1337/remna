"""Баланс: история, пополнение CryptoBot / Platega (MarkdownV2)."""

from __future__ import annotations

import logging
from datetime import timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.keyboards.inline import topup_amounts_keyboard, topup_invoice_keyboard, topup_providers_keyboard
from bot.states.payment import TopupStates
from bot.utils.screen_photo import (
    answer_callback_with_photo_screen,
    delete_message_safe,
    send_profile_screen,
)
from shared.config import get_settings
from shared.md2 import bold, code, esc, join_lines, plain
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.services.topup_service import (
    create_topup_payment,
    manual_check_and_apply_topup,
    notify_topup_success,
)

logger = logging.getLogger(__name__)

router = Router(name="balance")

_MSK_TZ = ZoneInfo("Europe/Moscow")


def _can_cryptobot() -> bool:
    s = get_settings()
    return s.cryptobot_stub or bool(s.cryptobot_token.strip())


def _can_platega() -> bool:
    s = get_settings()
    return s.platega_stub or bool(s.platega_merchant_id.strip() and s.platega_secret_key.strip())


def _ru_payment_provider(name: str | None) -> str:
    if not name:
        return plain("—")
    key = name.strip().lower()
    mapping = {
        "cryptobot": plain("CryptoBot"),
        "platega": plain("Platega (карта / СБП)"),
    }
    return mapping.get(key, esc(name))


async def _history_lines(session: AsyncSession, user_id: int, limit: int = 6) -> list[str]:
    r = await session.execute(
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.type.in_(("topup", "promo_topup_bonus")),
            Transaction.status == "completed",
        )
        .order_by(Transaction.id.desc())
        .limit(limit)
    )
    rows = r.scalars().all()
    if not rows:
        return [plain("Успешных оплат пока нет.")]
    lines: list[str] = []
    for t in rows:
        amt_s = f"{t.amount:.2f}"
        dt = t.created_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_s = dt.astimezone(_MSK_TZ).strftime("%d.%m.%Y %H:%M")
        if t.type == "promo_topup_bonus":
            promo_code = str(t.payment_id or "—")
            lines.append(
                "• "
                + plain("Промокод бонус ")
                + bold(amt_s)
                + plain(" ₽ · ")
                + code(promo_code)
                + plain(" · ")
                + esc(dt_s)
                + plain(" МСК")
            )
        else:
            prov = _ru_payment_provider(t.payment_provider)
            lines.append(
                "• "
                + plain("Пополнение ")
                + bold(amt_s)
                + plain(" ₽ · ")
                + prov
                + plain(" · ")
                + esc(dt_s)
                + plain(" МСК")
            )
    return lines


def _balance_caption(user: User, history: list[str]) -> str:
    bal = f"{user.balance:.2f}"
    hist_block = "\n".join(history)
    return join_lines(
        "💰 " + bold("Баланс"),
        "",
        plain("На счёте: ") + bold(bal) + plain(" ₽"),
        "",
        bold("История пополнений и бонусов:") + "\n" + hist_block,
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
    text = join_lines(f"Пополнение на {bold(str(amt))} ₽", "", plain("Выберите способ оплаты:"))
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
    if cq.message is not None:
        await state.update_data(topup_prompt_message_id=cq.message.message_id)
    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="topup:cancel_fsm"))
    await _edit_or_send_balance(
        cq,
        caption=join_lines(
            plain("Введите сумму в рублях (целое число), от ")
            + bold("50")
            + plain(" до ")
            + bold("100000")
            + plain(":"),
        ),
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
    bot = message.bot
    chat_id = message.chat.id
    data = await state.get_data()
    prompt_mid = data.get("topup_prompt_message_id")

    async def _cleanup_input() -> None:
        await delete_message_safe(message)

    raw = (message.text or "").strip().replace(",", ".")
    try:
        d = Decimal(raw)
    except InvalidOperation:
        await _cleanup_input()
        if bot:
            await bot.send_message(chat_id, "Введите число, например 250.")
        return
    if d != d.to_integral_value():
        await _cleanup_input()
        if bot:
            await bot.send_message(chat_id, "Укажите целое число рублей.")
        return
    amt = int(d)
    if amt < 50 or amt > 100_000:
        await _cleanup_input()
        if bot:
            await bot.send_message(chat_id, "Допустимо от 50 до 100000 ₽.")
        return

    await state.clear()
    await _cleanup_input()
    if bot and prompt_mid is not None:
        try:
            await bot.delete_message(chat_id, int(prompt_mid))
        except Exception:
            logger.debug("delete topup prompt message failed", exc_info=True)

    settings = get_settings()
    cap = join_lines(
        f"Пополнение на {bold(str(amt))} ₽",
        "",
        plain("Выберите способ оплаты:"),
    )
    if bot:
        await send_profile_screen(
            bot,
            chat_id=chat_id,
            caption=cap,
            reply_markup=topup_providers_keyboard(amt),
            settings=settings,
            delete_message=None,
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
        txn, pay_url = await create_topup_payment(
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
    label = "CryptoBot" if prov_name == "cryptobot" else "Platega"
    text = join_lines(
        plain("💳 Счёт через ")
        + bold(label)
        + plain(" на ")
        + bold(amount_s)
        + plain(" ₽ создан."),
        "",
        plain("Нажмите кнопку ниже, чтобы перейти к оплате."),
        "",
        plain("Пожалуйста, не закрывайте это окно, пока платёж не зачислится."),
        plain("После оплаты баланс обновится автоматически (обычно в течение минуты)."),
        plain("Если не обновилось — нажмите «Проверить зачисление»."),
    )
    if cq.message:
        if cq.message.photo:
            await cq.message.edit_caption(
                caption=text, reply_markup=topup_invoice_keyboard(pay_url, txn_id=txn.id)
            )
        else:
            await cq.message.edit_text(text, reply_markup=topup_invoice_keyboard(pay_url, txn_id=txn.id))


@router.callback_query(F.data.startswith("topup:check:"))
async def cb_topup_manual_check(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    try:
        txn_id = int(cq.data.split(":")[2])
    except Exception:
        await cq.answer("Ошибка проверки", show_alert=True)
        return

    settings = get_settings()
    status, credited, promo_bonus, should_notify = await manual_check_and_apply_topup(
        session, txn_id=txn_id, settings=settings
    )
    await session.commit()

    if status == "completed" and credited is not None:
        if should_notify:
            txn_row = await session.get(Transaction, txn_id)
            tg = cq.from_user.id if cq.from_user else db_user.telegram_id
            await notify_topup_success(
                telegram_id=tg,
                amount_rub=credited,
                promo_bonus_rub=promo_bonus,
                settings=settings,
                user_id=db_user.id,
                provider_name=(txn_row.payment_provider if txn_row else None),
            )
        await cq.answer()
        cap = join_lines(
            "✅ " + bold("Платёж зачислен"),
            "",
            plain("Зачислено: ") + bold(str(credited)) + plain(" ₽"),
        )
        await _edit_or_send_balance(
            cq,
            caption=cap,
            reply_markup=topup_amounts_keyboard(),
        )
        return

    if status in ("pending",):
        await cq.answer("Платёж пока не подтверждён. Попробуйте через 10–30 сек.", show_alert=False)
        return

    if status == "not_found":
        await cq.answer("Транзакция не найдена.", show_alert=True)
        return

    await cq.answer("Не удалось проверить платёж. Попробуйте позже.", show_alert=True)
