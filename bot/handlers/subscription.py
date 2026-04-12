"""Подписка: детальный экран, тарифы, меню продления."""

from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
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
from shared.config import Settings, get_settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError, subscription_url_for_telegram
from shared.services.optimized_route_service import (
    optimized_route_panel_ready,
    sync_user_optimized_route_to_panel,
)
from shared.md2 import bold, code, esc, italic, join_lines, plain, strip_for_popup_alert
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.models.plan import Plan
from shared.subscription_qr import subscription_url_qr_png
from shared.services.subscription_service import (
    BASE_SUBSCRIPTION_PLAN_NAME,
    TRIAL_PLAN_NAME,
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
    billing_zoneinfo,
)
from shared.services.billing_v2.detail_service import (
    get_month_summaries,
    get_today_summary,
    list_completed_transactions_billing_local_range,
    month_bounds,
    summarize_month_total,
    transaction_detail_bucket,
    usage_package_breakdown,
    user_has_tariff_subscription_charges,
)
from shared.services.promo_service import get_pending_purchase_discount_info
router = Router(name="subscription")


def _sub_main_keyboard(
    *,
    has_active: bool,
    subscription_url: str | None = None,
    show_billing_detail: bool = False,
    show_optimized_toggle: bool = False,
    optimized_on: bool = False,
    show_reissue_subscription: bool = False,
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
        b.row(InlineKeyboardButton(text="📋 Тарифы", callback_data="sub:plans"))
        if show_billing_detail:
            b.row(InlineKeyboardButton(text="📊 Детализация", callback_data="sub:detail:menu"))
        if show_optimized_toggle:
            label = "🛰 Оптим. маршрут: вкл" if optimized_on else "🛰 Оптим. маршрут: выкл"
            b.row(InlineKeyboardButton(text=label[:64], callback_data="sub:opt_route:toggle"))
        if show_reissue_subscription:
            b.row(
                InlineKeyboardButton(
                    text="🔑 Перевыпустить ключи подключения",
                    callback_data="sub:reissue:ask",
                )
            )
        b.row(InlineKeyboardButton(text="🔄 Продление подписки", callback_data="sub:renewal_menu"))
    else:
        b.row(
            InlineKeyboardButton(text="📋 Тарифы", callback_data="sub:plans"),
            InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance"),
        )
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main"))
    return b


def _sub_main_markup(
    settings: Settings,
    db_user: User,
    *,
    has_active: bool,
    subscription_url: str | None,
) -> InlineKeyboardMarkup:
    show = (
        settings.billing_v2_enabled
        and db_user.billing_mode == "hybrid"
        and has_active
        and optimized_route_panel_ready(settings)
    )
    show_detail = settings.billing_v2_enabled and db_user.billing_mode == "hybrid" and has_active
    show_reissue = bool(has_active and db_user.remnawave_uuid is not None)
    return _sub_main_keyboard(
        has_active=has_active,
        subscription_url=subscription_url,
        show_billing_detail=show_detail,
        show_optimized_toggle=show,
        optimized_on=db_user.optimized_route_enabled,
        show_reissue_subscription=show_reissue,
    ).as_markup()


def _detail_menu_keyboard(*, show_tariff_tab: bool) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📅 За сегодня", callback_data="sub:detail:today"))
    b.row(InlineKeyboardButton(text="🗓 За месяц", callback_data="sub:detail:month"))
    if show_tariff_tab:
        b.row(InlineKeyboardButton(text="💎 Тариф / абонемент", callback_data="sub:detail:tariff:menu"))
    b.row(InlineKeyboardButton(text="⬅️ Назад к подписке", callback_data="menu:sub_main"))
    return b


def _detail_tariff_menu_keyboard() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📅 Тариф: сегодня", callback_data="sub:detail:tariff:today"))
    b.row(InlineKeyboardButton(text="🗓 Тариф: месяц", callback_data="sub:detail:tariff:month"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="sub:detail:menu"))
    return b


def _txn_stamp(settings: Settings, dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(billing_zoneinfo(settings)).strftime("%d.%m %H:%M")


def _txn_title_for_detail(txn: Transaction) -> str:
    labels = {
        "admin_balance_add": "Начисление администратором",
        "topup": "Пополнение баланса",
        "promo_topup_bonus": "Бонус к пополнению (промокод)",
        "first_topup_balance_bonus": "Бонус первого пополнения",
        "referral_signup": "Реферал: регистрация",
        "referral_signup_invited": "Реферал: приглашённый",
        "referral_payment_percent": "Реферал: процент с оплаты",
        "usage_charge": "PAYG (трафик / устройства)",
        "subscription": "Тариф (покупка с баланса)",
        "subscription_autorenew": "Тариф (автопродление)",
        "manual_add": "Дополнительное устройство",
        "billing_transition": "Переход на гибридный биллинг",
    }
    base = labels.get(txn.type, txn.type)
    if txn.description and txn.type in ("usage_charge", "subscription", "subscription_autorenew"):
        short = txn.description.strip()
        if len(short) > 48:
            short = short[:45] + "…"
        return f"{base}: {short}"
    return base


def _append_transaction_detail_lines(
    parts: list[str],
    settings: Settings,
    txns: list[Transaction],
    *,
    max_lines: int = 22,
) -> None:
    credits: list[Transaction] = []
    debits: list[Transaction] = []
    for t in txns:
        b = transaction_detail_bucket(t)
        if b == "credit":
            credits.append(t)
        elif b == "debit":
            debits.append(t)
    if not credits and not debits:
        parts.extend(["", plain("Движения по балансу за период: нет записей.")])
        return
    n = 0

    def dump_block(title: str, items: list[Transaction], sign: str) -> None:
        nonlocal n
        if not items:
            return
        parts.extend(["", plain(title)])
        for t in items:
            if n >= max_lines:
                parts.append(italic("… остальные строки сокращены"))
                return
            stamp = _txn_stamp(settings, t.created_at)
            title_h = esc(_txn_title_for_detail(t))
            amt = str(t.amount.quantize(Decimal("0.01")))
            parts.append(plain(f"· {stamp} ") + bold(sign + amt) + plain(" ₽ — ") + title_h)
            n += 1

    dump_block("Зачисления на баланс:", credits, "+")
    dump_block("Списания и тарифы:", debits, "−")


def _plan_buyable_from_bot_catalog(plan: Plan | None) -> bool:
    """Те же ограничения, что и у `list_paid_plans` + явный запрет системных имён."""
    if plan is None or not plan.is_active or plan.price_rub <= 0:
        return False
    if plan.name in (BASE_SUBSCRIPTION_PLAN_NAME, TRIAL_PLAN_NAME):
        return False
    return True


async def _render_tariff_list(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    state: FSMContext,
    *,
    banner: str | None = None,
) -> None:
    """Список тарифов для покупки (кнопки всегда из актуального `list_paid_plans`)."""
    settings = get_settings()
    plans = await list_paid_plans(session)
    if not plans:
        await safe_callback_answer(cq, "Нет доступных тарифов", show_alert=True)
        return
    has_act = await get_active_subscription(session, db_user.id) is not None
    data = await state.get_data()
    is_extend = bool(data.get("sub_tariffs_extend"))
    if is_extend and has_act:
        title = "🔄 " + bold("Продлить подписку") + "\n\n"
    else:
        title = "📋 " + bold("Тарифы") + "\n\n"
    promo_code, discount_percent = await get_pending_purchase_discount_info(session, user_id=db_user.id)
    body = title + plain(
        "Выберите тариф (оплата с баланса). При нехватке средств тариф попадёт в корзину."
    )
    if banner:
        body = join_lines(banner, "", body)
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
    kb = _sub_main_markup(
        settings,
        db_user,
        has_active=sub is not None,
        subscription_url=sub_url,
    )
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


@router.callback_query(F.data == "sub:reissue:ask")
async def cb_sub_reissue_ask(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    sub = await get_active_subscription(session, db_user.id)
    if sub is None or db_user.remnawave_uuid is None:
        await cq.answer("Нет активной подписки или учётной записи VPN.", show_alert=True)
        return
    settings = get_settings()
    cap = join_lines(
        "🔑 " + bold("Перевыпуск ключей подключения"),
        "",
        plain("Панель Remnawave перевыпустит ключи и ссылку подписки: старые перестанут работать."),
        plain("Обновите подписку во всех приложениях VPN."),
        "",
        bold("Продолжить?"),
    )
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Да, перевыпустить", callback_data="sub:reissue:do"),
        InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu:sub_main"),
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "sub:reissue:do")
async def cb_sub_reissue_do(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    sub = await get_active_subscription(session, db_user.id)
    if sub is None or db_user.remnawave_uuid is None:
        await cq.answer("Нет активной подписки или учётной записи VPN.", show_alert=True)
        return
    settings = get_settings()
    rw = RemnaWaveClient(settings)
    uid = str(db_user.remnawave_uuid)
    try:
        uinf = await rw.reset_user_subscription_credentials(uid, revoke_only_passwords=False)
    except RemnaWaveError as e:
        await cq.answer(strip_for_popup_alert(str(e))[:200], show_alert=True)
        return
    raw_url = uinf.get("subscriptionUrl")
    new_url = subscription_url_for_telegram(raw_url if isinstance(raw_url, str) else None, settings)
    cap_ok = join_lines(
        "✅ " + bold("Ключи и ссылка обновлены"),
        "",
        plain("Новая ссылка подписки:"),
        code(new_url) if new_url else plain("—"),
        "",
        plain("Импортируйте её в приложении VPN и удалите старую подписку, если она ещё отображается."),
    )
    b = InlineKeyboardBuilder()
    if new_url:
        b.row(InlineKeyboardButton(text="📎 Открыть ссылку", url=new_url))
    b.row(InlineKeyboardButton(text="⬅️ К экрану подписки", callback_data="menu:sub_main"))
    await answer_callback_with_photo_screen(
        cq,
        caption=cap_ok,
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "sub:opt_route:toggle")
async def cb_opt_route_toggle(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    if not settings.billing_v2_enabled or db_user.billing_mode != "hybrid":
        await cq.answer("Доступно только в гибридном биллинге.", show_alert=True)
        return
    if not optimized_route_panel_ready(settings):
        await cq.answer(
            "Не заданы REMNAWAVE_DEFAULT_SQUAD_UUID и REMNAWAVE_OPTIMIZED_SQUAD_UUID в настройках сервера.",
            show_alert=True,
        )
        return
    if db_user.remnawave_uuid is None:
        await cq.answer("Нет учётной записи VPN в панели.", show_alert=True)
        return
    prev = db_user.optimized_route_enabled
    db_user.optimized_route_enabled = not prev
    await session.flush()
    try:
        await sync_user_optimized_route_to_panel(user=db_user, settings=settings)
    except RemnaWaveError as e:
        db_user.optimized_route_enabled = prev
        await session.flush()
        await cq.answer(strip_for_popup_alert(str(e))[:200], show_alert=True)
        return
    await _show_subscription_main(cq, session, db_user, is_bot_admin=is_bot_admin)


@router.callback_query(F.data.in_(("sub:plans", "sub:extend")))
async def cb_plans_or_extend(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    is_extend = cq.data == "sub:extend"
    await state.update_data(sub_tariffs_extend=is_extend)
    await _render_tariff_list(cq, session, db_user, state, banner=None)


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

    plan = await session.get(Plan, pid)
    if not _plan_buyable_from_bot_catalog(plan):
        await _render_tariff_list(
            cq,
            session,
            db_user,
            state,
            banner=plain("Этот тариф снят с продажи или удалён — список обновлён."),
        )
        return
    assert plan is not None
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
        kb = _sub_main_markup(
            settings,
            db_user,
            has_active=sub is not None,
            subscription_url=sub_url,
        )
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
async def cb_detail_menu(cq: CallbackQuery, session: AsyncSession, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    if not settings.billing_v2_enabled or db_user.billing_mode != "hybrid":
        await cq.answer("Детализация доступна в гибридном биллинге (v2).", show_alert=True)
        return
    show_tar = await user_has_tariff_subscription_charges(session, db_user.id)
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines("📊 " + bold("Детализация расходов"), "", plain("Выберите период.")),
        reply_markup=_detail_menu_keyboard(show_tariff_tab=show_tar).as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "sub:detail:today")
async def cb_detail_today(cq: CallbackQuery, session: AsyncSession, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    if not settings.billing_v2_enabled or db_user.billing_mode != "hybrid":
        await cq.answer("Детализация доступна в гибридном биллинге (v2).", show_alert=True)
        return
    today = billing_today(settings)
    row = await get_today_summary(session, user_id=db_user.id, today=today)
    from_dt = billing_local_day_start_utc(settings, today)
    to_dt = billing_local_day_end_utc_exclusive(settings, today)
    pack = await usage_package_breakdown(session, user_id=db_user.id, from_dt=from_dt, to_dt=to_dt)
    if row is None:
        head = join_lines("📅 " + bold("Сегодня"), "", plain("Сводка PAYG за сегодня пуста."))
    else:
        head = join_lines(
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
    txns = await list_completed_transactions_billing_local_range(
        session,
        user_id=db_user.id,
        settings=settings,
        from_day=today,
        to_day_inclusive=today,
        tariff_only=False,
    )
    extra: list[str] = []
    _append_transaction_detail_lines(extra, settings, txns)
    text = join_lines(head, *extra) if extra else head
    show_tar = await user_has_tariff_subscription_charges(session, db_user.id)
    await answer_callback_with_photo_screen(
        cq,
        caption=text,
        reply_markup=_detail_menu_keyboard(show_tariff_tab=show_tar).as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "sub:detail:month")
async def cb_detail_month(cq: CallbackQuery, session: AsyncSession, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    if not settings.billing_v2_enabled or db_user.billing_mode != "hybrid":
        await cq.answer("Детализация доступна в гибридном биллинге (v2).", show_alert=True)
        return
    anchor = billing_today(settings)
    rows = await get_month_summaries(session, user_id=db_user.id, anchor_day=anchor)
    month_start, next_month = month_bounds(anchor)
    month_from = billing_local_day_start_utc(settings, month_start)
    month_to = billing_local_day_start_utc(settings, next_month)
    pack = await usage_package_breakdown(session, user_id=db_user.id, from_dt=month_from, to_dt=month_to)
    last_cal_day = next_month - timedelta(days=1)
    if not rows:
        head = join_lines("🗓 " + bold("За месяц"), "", plain("Сводка PAYG за месяц пуста."))
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
        head = join_lines(*lines)
    txns_m = await list_completed_transactions_billing_local_range(
        session,
        user_id=db_user.id,
        settings=settings,
        from_day=month_start,
        to_day_inclusive=last_cal_day,
        tariff_only=False,
    )
    extra_m: list[str] = []
    _append_transaction_detail_lines(extra_m, settings, txns_m, max_lines=28)
    text = join_lines(head, *extra_m) if extra_m else head
    show_tar = await user_has_tariff_subscription_charges(session, db_user.id)
    await answer_callback_with_photo_screen(
        cq,
        caption=text,
        reply_markup=_detail_menu_keyboard(show_tariff_tab=show_tar).as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "sub:detail:tariff:menu")
async def cb_detail_tariff_menu(cq: CallbackQuery, session: AsyncSession, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    if not settings.billing_v2_enabled or db_user.billing_mode != "hybrid":
        await cq.answer("Детализация доступна в гибридном биллинге (v2).", show_alert=True)
        return
    if not await user_has_tariff_subscription_charges(session, db_user.id):
        await cq.answer("Нет оплат тарифа с баланса.", show_alert=True)
        return
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "💎 " + bold("Тариф / абонемент"),
            "",
            plain("Покупки тарифа и автопродление с баланса за выбранный период."),
        ),
        reply_markup=_detail_tariff_menu_keyboard().as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "sub:detail:tariff:today")
async def cb_detail_tariff_today(cq: CallbackQuery, session: AsyncSession, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    if not settings.billing_v2_enabled or db_user.billing_mode != "hybrid":
        await cq.answer("Детализация доступна в гибридном биллинге (v2).", show_alert=True)
        return
    if not await user_has_tariff_subscription_charges(session, db_user.id):
        await cq.answer("Нет оплат тарифа с баланса.", show_alert=True)
        return
    today = billing_today(settings)
    txns = await list_completed_transactions_billing_local_range(
        session,
        user_id=db_user.id,
        settings=settings,
        from_day=today,
        to_day_inclusive=today,
        tariff_only=True,
    )
    lines: list[str] = [
        "💎 " + bold(f"Тариф за сегодня | {today.strftime('%d.%m.%Y')}"),
        "",
    ]
    if not txns:
        lines.append(plain("Записей за сегодня нет."))
    else:
        for t in txns[:20]:
            stamp = _txn_stamp(settings, t.created_at)
            title_h = esc(_txn_title_for_detail(t))
            amt = str(t.amount.quantize(Decimal("0.01")))
            lines.append(plain(f"· {stamp} ") + bold("−" + amt) + plain(" ₽ — ") + title_h)
    text = join_lines(*lines)
    await answer_callback_with_photo_screen(
        cq,
        caption=text,
        reply_markup=_detail_tariff_menu_keyboard().as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "sub:detail:tariff:month")
async def cb_detail_tariff_month(cq: CallbackQuery, session: AsyncSession, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    if not settings.billing_v2_enabled or db_user.billing_mode != "hybrid":
        await cq.answer("Детализация доступна в гибридном биллинге (v2).", show_alert=True)
        return
    if not await user_has_tariff_subscription_charges(session, db_user.id):
        await cq.answer("Нет оплат тарифа с баланса.", show_alert=True)
        return
    anchor = billing_today(settings)
    month_start, next_month = month_bounds(anchor)
    last_cal_day = next_month - timedelta(days=1)
    txns = await list_completed_transactions_billing_local_range(
        session,
        user_id=db_user.id,
        settings=settings,
        from_day=month_start,
        to_day_inclusive=last_cal_day,
        tariff_only=True,
    )
    lines: list[str] = [
        "💎 " + bold("Тариф за календарный месяц"),
        "",
    ]
    if not txns:
        lines.append(plain("Записей за месяц нет."))
    else:
        for t in txns[:40]:
            stamp = _txn_stamp(settings, t.created_at)
            title_h = esc(_txn_title_for_detail(t))
            amt = str(t.amount.quantize(Decimal("0.01")))
            lines.append(plain(f"· {stamp} ") + bold("−" + amt) + plain(" ₽ — ") + title_h)
    text = join_lines(*lines)
    await answer_callback_with_photo_screen(
        cq,
        caption=text,
        reply_markup=_detail_tariff_menu_keyboard().as_markup(),
        settings=settings,
    )
