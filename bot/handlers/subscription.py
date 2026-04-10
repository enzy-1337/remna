"""Подписка: детальный экран, тарифы, меню продления."""

from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
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
from shared.models.plan import Plan
from shared.subscription_qr import subscription_url_qr_png
from shared.services.subscription_service import (
    calculate_discounted_plan_price,
    get_active_subscription,
    list_paid_plans,
    plan_tariff_button_label,
    plan_tariff_button_label_with_discount,
    purchase_plan_with_balance,
    set_subscription_auto_renew,
)

from shared.services.billing_v2.billing_calendar import (
    billing_local_day_end_utc_exclusive,
    billing_local_day_start_utc,
    billing_today,
)
from shared.services.billing_v2.detail_service import (
    get_month_summaries,
    get_today_summary,
    month_bounds,
    summarize_month_total,
    usage_package_breakdown,
)
from shared.services.promo_service import get_pending_purchase_discount_info
router = Router(name="subscription")


def _sub_main_keyboard(
    *,
    has_active: bool,
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
        b.row(InlineKeyboardButton(text="📊 Детализация", callback_data="sub:detail:menu"))
        b.row(InlineKeyboardButton(text="🔄 Продление подписки", callback_data="sub:renewal_menu"))
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main"))
    return b


def _detail_menu_keyboard() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📅 За сегодня", callback_data="sub:detail:today"))
    b.row(InlineKeyboardButton(text="🗓 За месяц", callback_data="sub:detail:month"))
    b.row(InlineKeyboardButton(text="⬅️ Назад к подписке", callback_data="menu:sub_main"))
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
    if is_extend and has_act:
        title = "🔄 " + bold("Продлить подписку") + "\n\n"
    else:
        title = "📋 " + bold("Тарифы") + "\n\n"
    promo_code, discount_percent = await get_pending_purchase_discount_info(session, user_id=db_user.id)
    body = title + plain(
        "Выберите тариф (оплата с баланса). При нехватке средств тариф попадёт в корзину."
    )
    if discount_percent > 0 and promo_code:
        body = join_lines(
            body,
            "",
            plain("🎟 Активная скидка: ")
            + bold(str(discount_percent))
            + plain("% по коду ")
            + code(promo_code)
            + plain(" (применится к следующей покупке тарифа)."),
        )
    b = InlineKeyboardBuilder()
    for p in plans:
        label = (
            plan_tariff_button_label_with_discount(p, discount_percent)
            if discount_percent > 0
            else plan_tariff_button_label(p)
        )
        b.row(
            InlineKeyboardButton(
                text=label[:64],
                callback_data=f"sub:buy:{p.id}",
            )
        )
    back_cb = "menu:sub_main" if has_act else "menu:main"
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
    state: FSMContext,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    try:
        pid = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Ошибка тарифа", show_alert=True)
        return

    from shared.models.plan import Plan
    plan = await session.get(Plan, pid)
    if plan is None or not plan.is_active or plan.price_rub <= 0:
        await cq.answer("Тариф недоступен", show_alert=True)
        return
    promo_code, discount_percent = await get_pending_purchase_discount_info(session, user_id=db_user.id)
    original, discount_amount, final = calculate_discounted_plan_price(plan, discount_percent)

    lines = [
        "🧾 " + bold("Подтверждение покупки"),
        "",
        plain("Тариф: ") + bold(plan.name),
        plain("Цена: ") + bold(str(original)) + plain(" ₽"),
    ]
    if discount_percent > 0 and promo_code:
        lines.append(
            plain("Скидка: ")
            + bold(str(discount_percent))
            + plain("% по коду ")
            + code(promo_code)
            + plain(" (−")
            + bold(str(discount_amount))
            + plain(" ₽)")
        )
    lines.append(plain("К списанию: ") + bold(str(final)) + plain(" ₽"))
    lines.append("")
    lines.append(plain("Продолжить покупку?"))

    token = secrets.token_urlsafe(8)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"sub:buyconfirm:{pid}:{token}"))
    b.row(InlineKeyboardButton(text="⬅️ К тарифам", callback_data="sub:plans"))
    await state.update_data(sub_buy_confirm_token=token, sub_buy_confirm_plan_id=pid, sub_buy_confirm_expires_at=expires_at)
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(*lines),
        reply_markup=b.as_markup(),
        settings=get_settings(),
    )


@router.callback_query(F.data.startswith("sub:buyconfirm:"))
async def cb_buy_plan_confirm(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    try:
        parts = cq.data.split(":")
        pid = int(parts[2])
        token = parts[3]
    except (IndexError, ValueError):
        await cq.answer("Ошибка тарифа", show_alert=True)
        return
    data = await state.get_data()
    exp_raw = data.get("sub_buy_confirm_expires_at")
    valid_token = data.get("sub_buy_confirm_token")
    valid_pid = data.get("sub_buy_confirm_plan_id")
    if not isinstance(valid_token, str) or token != valid_token or valid_pid != pid:
        await cq.answer("Подтверждение уже использовано или устарело.", show_alert=True)
        return
    if isinstance(exp_raw, str):
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(exp_raw):
                await cq.answer("Время подтверждения истекло. Повторите выбор тарифа.", show_alert=True)
                return
        except ValueError:
            pass
    await state.update_data(sub_buy_confirm_token=None, sub_buy_confirm_plan_id=None, sub_buy_confirm_expires_at=None)
    settings = get_settings()
    tid = cq.from_user.id if cq.from_user else db_user.telegram_id
    ok, msg, kind = await purchase_plan_with_balance(
        session,
        user=db_user,
        plan_id=pid,
        telegram_id=tid,
        settings=settings,
        save_to_cart_if_insufficient=True,
        idempotency_key=f"subbuy:{db_user.id}:{token}",
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


@router.callback_query(F.data == "sub:renewal_menu")
async def cb_renewal_menu(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    sub = await get_active_subscription(session, db_user.id)
    if not sub:
        await cq.answer("Нет активной подписки.", show_alert=True)
        return
    plan = await session.get(Plan, sub.plan_id) if sub.plan_id else None
    monthly_price = str(plan.price_rub) if plan is not None else "—"
    auto_text = "включено ✅" if sub.auto_renew else "выключено ⏸"
    cap = join_lines(
        "🔄 " + bold("Продление подписки"),
        "",
        plain("Здесь можно продлить подписку или переключить автопродление."),
        plain("Стоимость продления в месяц: ") + bold(monthly_price) + plain(" ₽"),
        plain("Текущее автопродление: ") + bold(auto_text),
    )
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="💳 Продлить подписку", callback_data="sub:extend"))
    toggle_text = "⏸ Выключить автопродление" if sub.auto_renew else "▶️ Включить автопродление"
    b.row(InlineKeyboardButton(text=toggle_text, callback_data="sub:renewal_toggle"))
    b.row(InlineKeyboardButton(text="⬅️ Назад к подписке", callback_data="menu:sub_main"))
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=b.as_markup(),
        settings=get_settings(),
    )


@router.callback_query(F.data == "sub:renewal_toggle")
async def cb_renewal_toggle(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
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
    await cb_renewal_menu(cq, session, db_user)


@router.callback_query(F.data == "sub:detail:menu")
async def cb_detail_menu(cq: CallbackQuery, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    settings = get_settings()
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines("📊 " + bold("Детализация расходов"), "", plain("Выберите период.")),
        reply_markup=_detail_menu_keyboard().as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "sub:detail:today")
async def cb_detail_today(cq: CallbackQuery, session: AsyncSession, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    today = billing_today(settings)
    row = await get_today_summary(session, user_id=db_user.id, today=today)
    from_dt = billing_local_day_start_utc(settings, today)
    to_dt = billing_local_day_end_utc_exclusive(settings, today)
    pack = await usage_package_breakdown(session, user_id=db_user.id, from_dt=from_dt, to_dt=to_dt)
    if row is None:
        text = join_lines("📅 " + bold("Сегодня"), "", plain("Списаний пока нет."))
    else:
        text = join_lines(
            "📅 " + bold(f"Сегодня | {row.day.strftime('%d.%m.%Y')}"),
            plain("За гигабайты: ")
            + bold(str(row.gb_units))
            + plain(" × ")
            + bold(str(settings.billing_gb_step_rub))
            + plain(" ₽ = ")
            + bold(str(row.gb_amount_rub))
            + plain(" ₽"),
            plain("За устройства: ")
            + bold(str(row.device_units))
            + plain(" × ")
            + bold(str(settings.billing_device_daily_rub))
            + plain(" ₽ = ")
            + bold(str(row.device_amount_rub))
            + plain(" ₽"),
            plain("За Мобильный интернет: ")
            + bold(str(row.mobile_gb_units))
            + plain(" × ")
            + bold(str(settings.billing_mobile_gb_extra_rub))
            + plain(" ₽ = ")
            + bold(str(row.mobile_amount_rub))
            + plain(" ₽"),
            "",
            plain("В общем: ") + bold(str(row.total_amount_rub)) + plain(" ₽ за сегодня"),
            "",
            plain("Покрыто пакетом: ГБ ")
            + bold(str(pack["gb_covered"]))
            + plain(", устройства ")
            + bold(str(pack["device_covered"])),
            plain("Списано сверх пакета: ГБ ")
            + bold(str(pack["gb_charged"]))
            + plain(", устройства ")
            + bold(str(pack["device_charged"])),
        )
    await answer_callback_with_photo_screen(
        cq,
        caption=text,
        reply_markup=_detail_menu_keyboard().as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "sub:detail:month")
async def cb_detail_month(cq: CallbackQuery, session: AsyncSession, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    anchor = billing_today(settings)
    rows = await get_month_summaries(session, user_id=db_user.id, anchor_day=anchor)
    month_start, next_month = month_bounds(anchor)
    month_from = billing_local_day_start_utc(settings, month_start)
    month_to = billing_local_day_start_utc(settings, next_month)
    pack = await usage_package_breakdown(session, user_id=db_user.id, from_dt=month_from, to_dt=month_to)
    if not rows:
        text = join_lines("🗓 " + bold("За месяц"), "", plain("Списаний пока нет."))
    else:
        lines = ["🗓 " + bold("За месяц"), ""]
        for row in rows[:31]:
            lines.append(
                plain(row.day.strftime("%d.%m.%Y"))
                + plain(": ")
                + bold(str(row.total_amount_rub))
                + plain(" ₽")
            )
        lines.extend(["", plain("Итого: ") + bold(str(summarize_month_total(rows))) + plain(" ₽")])
        lines.extend(
            [
                "",
                plain("Покрыто пакетом: ГБ ")
                + bold(str(pack["gb_covered"]))
                + plain(", устройства ")
                + bold(str(pack["device_covered"])),
                plain("Списано сверх пакета: ГБ ")
                + bold(str(pack["gb_charged"]))
                + plain(", устройства ")
                + bold(str(pack["device_charged"])),
            ]
        )
        text = join_lines(*lines)
    await answer_callback_with_photo_screen(
        cq,
        caption=text,
        reply_markup=_detail_menu_keyboard().as_markup(),
        settings=settings,
    )
