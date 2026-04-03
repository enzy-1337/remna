"""Подписка: детальный экран, тарифы, покупка, авто-продление."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.keyboards.instructions_kb import build_instructions_markup
from bot.ui.subscription_detail import build_subscription_detail_caption
from bot.utils.screen_photo import (
    answer_callback_with_photo_screen,
    delete_message_safe,
    safe_callback_answer,
)
from shared.config import get_settings
from shared.md2 import bold, code, join_lines, plain, strip_for_popup_alert
from shared.models.user import User
from shared.subscription_qr import subscription_url_qr_png
from shared.services.subscription_service import (
    get_active_subscription,
    list_paid_plans,
    plan_tariff_button_label,
    purchase_plan_with_balance,
    set_subscription_auto_renew,
)
router = Router(name="subscription")


def _sub_main_keyboard(
    *,
    has_active: bool,
    auto_renew: bool,
    subscription_url: str | None = None,
) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    if has_active:
        if subscription_url:
            b.row(
                InlineKeyboardButton(text="📎 Подключиться", url=subscription_url),
                InlineKeyboardButton(text="🔳 QR-код", callback_data="sub:qr"),
            )
        b.row(
            InlineKeyboardButton(text="🖥 Устройства", callback_data="sub:devices"),
            InlineKeyboardButton(text="📖 Инструкции", callback_data="sub:instr"),
        )
        b.row(InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="sub:extend"))
        ar_text = "⏸ Авто-продление: вкл" if auto_renew else "▶️ Авто-продление: выкл"
        b.row(InlineKeyboardButton(text=ar_text, callback_data="sub:toggle_ar"))
    b.row(InlineKeyboardButton(text="🎁 Промокод", callback_data="menu:promo"))
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main"))
    return b


async def _show_subscription_main(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    *,
    is_bot_admin: bool = False,
) -> None:
    settings = get_settings()
    cap, sub_url = await build_subscription_detail_caption(
        session, user=db_user, settings=settings, is_bot_admin=is_bot_admin
    )
    sub = await get_active_subscription(session, db_user.id)
    kb = _sub_main_keyboard(
        has_active=sub is not None,
        auto_renew=sub.auto_renew if sub else False,
        subscription_url=sub_url,
    ).as_markup()
    await answer_callback_with_photo_screen(cq, caption=cap, reply_markup=kb, settings=settings)


@router.callback_query(F.data == "sub:qr")
async def cb_sub_qr(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    _cap, sub_url = await build_subscription_detail_caption(
        session, user=db_user, settings=settings, is_bot_admin=is_bot_admin
    )
    if not sub_url:
        await cq.answer("Нет ссылки подписки", show_alert=True)
        return
    try:
        png = subscription_url_qr_png(sub_url)
    except ValueError:
        await cq.answer("Нет ссылки подписки", show_alert=True)
        return
    if cq.message is None or cq.bot is None:
        return
    chat_id = cq.message.chat.id
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⬅️ Назад в подписку", callback_data="menu:sub_main"))
    cap = join_lines("🔳 " + bold("QR для подключения"), "", plain("Отсканируйте в приложении VPN."))
    await safe_callback_answer(cq)
    await delete_message_safe(cq.message)
    await cq.bot.send_photo(
        chat_id,
        BufferedInputFile(png, filename="subscription.png"),
        caption=cap,
        parse_mode="MarkdownV2",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.in_(("menu:sub_main", "menu:subscription")))
async def cb_subscription_main(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    await _show_subscription_main(cq, session, db_user, is_bot_admin=is_bot_admin)


@router.callback_query(F.data.in_(("sub:plans", "sub:extend")))
async def cb_plans_or_extend(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    plans = await list_paid_plans(session)
    if not plans:
        await cq.answer("Нет доступных тарифов", show_alert=True)
        return
    has_act = await get_active_subscription(session, db_user.id) is not None
    is_extend = cq.data == "sub:extend"
    title = (
        "🔄 " + bold("Продлить подписку") + "\n\n"
        if (is_extend and has_act)
        else "🛒 " + bold("Купить подписку") + "\n\n"
    )
    body = title + plain(
        "Выберите тариф (оплата с баланса). При нехватке средств тариф попадёт в корзину."
    )
    b = InlineKeyboardBuilder()
    for p in plans:
        b.row(
            InlineKeyboardButton(
                text=plan_tariff_button_label(p),
                callback_data=f"sub:buy:{p.id}",
            )
        )
    back_cb = "menu:sub_main" if has_act else "menu:main"
    b.row(InlineKeyboardButton(text="🎁 Промокод", callback_data="menu:promo"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb))
    await answer_callback_with_photo_screen(
        cq,
        caption=body,
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data.startswith("sub:buy:"))
async def cb_buy_plan(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    try:
        pid = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Ошибка тарифа", show_alert=True)
        return
    settings = get_settings()
    tid = cq.from_user.id if cq.from_user else db_user.telegram_id
    ok, msg, kind = await purchase_plan_with_balance(
        session,
        user=db_user,
        plan_id=pid,
        telegram_id=tid,
        settings=settings,
        save_to_cart_if_insufficient=True,
    )
    if not cq.message or cq.bot is None:
        return
    if ok:
        cap, sub_url = await build_subscription_detail_caption(
            session, user=db_user, settings=settings, is_bot_admin=is_bot_admin
        )
        full = msg + "\n\n" + cap
        sub = await get_active_subscription(session, db_user.id)
        kb = _sub_main_keyboard(
            has_active=sub is not None,
            auto_renew=sub.auto_renew if sub else False,
            subscription_url=sub_url,
        ).as_markup()
        await answer_callback_with_photo_screen(
            cq,
            caption=full,
            reply_markup=kb,
            settings=settings,
        )
        return
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ К тарифам", callback_data="sub:plans"))
    b.row(InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance"))
    has_act = await get_active_subscription(session, db_user.id) is not None
    b.row(
        InlineKeyboardButton(
            text="🔑 Подписка",
            callback_data="menu:sub_main" if has_act else "menu:main",
        )
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=msg,
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "sub:instr")
async def cb_sub_instructions(
    cq: CallbackQuery,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    settings = get_settings()
    kb = build_instructions_markup(
        settings,
        back_callback="menu:sub_main",
        back_text="⬅️ Назад к подписке",
    )
    text = join_lines(
        "📖 " + bold("Инструкции"),
        "",
        plain("Выберите платформу — откроется статья в Telegra.ph."),
    )
    if (
        not settings.instruction_telegraph_phone_url
        and not settings.instruction_telegraph_pc_url
        and not (
            settings.instruction_android_url
            or settings.instruction_ios_url
            or settings.instruction_windows_url
            or settings.instruction_macos_url
        )
    ):
        text += (
            "\n\n⚠️ Задайте "
            + code("INSTRUCTION_TELEGRAPH_PHONE_URL")
            + " и "
            + code("INSTRUCTION_TELEGRAPH_PC_URL")
            + plain(" в .env.")
        )
    await answer_callback_with_photo_screen(
        cq,
        caption=text,
        reply_markup=kb,
        settings=settings,
    )


@router.callback_query(F.data == "sub:toggle_ar")
async def cb_toggle_ar(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    sub = await get_active_subscription(session, db_user.id)
    if not sub:
        await cq.answer("Нет активной подписки.", show_alert=True)
        return
    new_val = not sub.auto_renew
    ok, tip = await set_subscription_auto_renew(session, db_user.id, new_val)
    if not ok:
        await cq.answer(strip_for_popup_alert(tip)[:200], show_alert=True)
        return
    await _show_subscription_main(cq, session, db_user, is_bot_admin=is_bot_admin)
