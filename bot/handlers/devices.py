"""Устройства: список из Remnawave HWID API, отвязка, покупка слотов (MarkdownV2)."""

from __future__ import annotations

import secrets

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.utils.screen_photo import answer_callback_with_photo_screen
from shared.config import get_settings
from shared.integrations.remnawave import RemnaWaveError
from shared.integrations.rw_hwid_devices import format_rw_device_datetime_local
from shared.md2 import bold, code, esc, join_lines, plain, strip_for_popup_alert
from shared.models.user import User
from shared.services.device_slots_pricing import (
    is_admin_unlimited_devices,
    price_for_extra_device_slots,
    slots_available_to_buy,
)
from shared.services.hwid_devices_service import (
    connected_devices_count,
    device_display_title,
    fetch_panel_hwid_context,
    panel_devices_unlimited,
)
from shared.services.subscription_service import (
    MIN_DEVICES,
    MAX_DEVICES,
    add_paid_device_slots,
    get_active_subscription,
    remove_hwid_device_from_panel,
    unlink_hwid_device_keep_slots,
)

router = Router(name="devices")

CTX_MAIN = "main"
CTX_SUB = "sub"


def _devices_back_cb(ctx: str) -> str:
    return "menu:sub_main" if ctx == CTX_SUB else "menu:main"


def _list_callback(ctx: str) -> str:
    return f"dev:list:{ctx}"


def _devices_kb(
    devices: list[dict],
    *,
    ctx: str,
    can_buy_slots: int,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, d in enumerate(devices):
        b.row(
            InlineKeyboardButton(
                text=device_display_title(d, i + 1),
                callback_data=f"dev:rw:{i}:{ctx}",
            )
        )
    if can_buy_slots > 0:
        b.row(
            InlineKeyboardButton(
                text="➕ Докупить слоты",
                callback_data=f"dev:buy:{ctx}",
                style="primary",
            )
        )
    b.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data=_devices_back_cb(ctx), style="danger")
    )
    return b.as_markup()


def _buy_qty_keyboard(ctx: str, max_qty: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    for n in range(1, max_qty + 1):
        row.append(
            InlineKeyboardButton(text=str(n), callback_data=f"dev:buyqty:{n}:{ctx}")
        )
        if len(row) >= 4:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=_list_callback(ctx), style="danger"))
    return b.as_markup()


async def _render_devices(
    session: AsyncSession,
    user: User,
    *,
    ctx: str,
    is_bot_admin: bool = False,
) -> tuple[str, object]:
    settings = get_settings()
    sub = await get_active_subscription(session, user.id)
    if not sub:
        kb = (
            InlineKeyboardBuilder()
            .row(
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=_devices_back_cb(ctx), style="danger"
                )
            )
            .as_markup()
        )
        return join_lines("🖥 " + bold("Устройства"), "", plain("Сначала оформите подписку или триал.")), kb

    uinf, devices, err = await fetch_panel_hwid_context(user, settings)

    if err:
        cap = join_lines(
            "🖥 " + bold("Устройства"),
            "",
            plain("Не удалось загрузить устройства из панели:"),
            esc(err),
            "",
            plain("Проверьте REMNAWAVE_API_TOKEN и доступ к API HWID."),
        )
        kb = (
            InlineKeyboardBuilder()
            .row(
                InlineKeyboardButton(
                    text="🔄 Обновить", callback_data=_list_callback(ctx)
                )
            )
            .row(
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=_devices_back_cb(ctx), style="danger"
                )
            )
            .as_markup()
        )
        return cap, kb

    used = connected_devices_count(uinf, devices)
    unlimited = panel_devices_unlimited(uinf, is_bot_admin=is_bot_admin) or is_admin_unlimited_devices(
        user, settings
    )
    denom = bold("∞") if unlimited else bold(str(sub.devices_count))
    can_buy = 0
    if not unlimited and not (
        settings.billing_v2_enabled and user.billing_mode == "hybrid"
    ):
        can_buy = slots_available_to_buy(int(sub.devices_count), MAX_DEVICES)

    slots_line = plain("📟 Привязано: ") + bold(str(used)) + plain(" / ") + denom
    if not unlimited:
        slots_line = join_lines(
            slots_line,
            plain("Лимит подписки: ") + bold(str(sub.devices_count)) + plain(f" (макс. {MAX_DEVICES})"),
        )
    lines = join_lines(
        "🖥 " + bold("Устройства"),
        "",
        slots_line,
        "",
        plain("Нажмите устройство для отвязки или удаления слота."),
    )
    if can_buy > 0:
        unit = settings.extra_device_price_rub
        lines = join_lines(
            lines,
            "",
            plain("Докупка слотов: от ")
            + bold(str(unit))
            + plain(" ₽/слот, скидка от 3 шт."),
        )
    return lines, _devices_kb(devices, ctx=ctx, can_buy_slots=can_buy)


async def _open_devices_screen(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    *,
    ctx: str,
    is_bot_admin: bool = False,
) -> None:
    settings = get_settings()
    text, kb = await _render_devices(session, db_user, ctx=ctx, is_bot_admin=is_bot_admin)
    await answer_callback_with_photo_screen(
        cq,
        caption=text,
        reply_markup=kb,
        settings=settings,
    )


@router.callback_query(F.data == "menu:devices")
async def cb_devices_main(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    await _open_devices_screen(cq, session, db_user, ctx=CTX_MAIN, is_bot_admin=is_bot_admin)


@router.callback_query(F.data == "sub:devices")
async def cb_devices_from_sub(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    await _open_devices_screen(cq, session, db_user, ctx=CTX_SUB, is_bot_admin=is_bot_admin)


@router.callback_query(F.data.startswith("dev:list:"))
async def cb_dev_list(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    ctx = parts[2] if len(parts) > 2 else CTX_MAIN
    await _open_devices_screen(cq, session, db_user, ctx=ctx, is_bot_admin=is_bot_admin)
    await cq.answer()


@router.callback_query(F.data.startswith("dev:buy:"))
async def cb_dev_buy_menu(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    ctx = cq.data.split(":")[2] if len(cq.data.split(":")) > 2 else CTX_MAIN
    settings = get_settings()
    if settings.billing_v2_enabled and db_user.billing_mode == "hybrid":
        await cq.answer("Для PAYG докупка слотов недоступна.", show_alert=True)
        return
    if is_admin_unlimited_devices(db_user, settings):
        await cq.answer("У администратора лимит устройств не ограничен.", show_alert=True)
        return
    sub = await get_active_subscription(session, db_user.id)
    if not sub:
        await cq.answer("Нет активной подписки.", show_alert=True)
        return
    can_buy = slots_available_to_buy(int(sub.devices_count), MAX_DEVICES)
    if can_buy <= 0:
        await cq.answer(f"Достигнут лимит {MAX_DEVICES} слотов.", show_alert=True)
        return
    cap = join_lines(
        "➕ " + bold("Докупить слоты"),
        "",
        plain("Выберите, сколько слотов добавить (макс. ")
        + bold(str(can_buy))
        + plain("):"),
        plain("Цена за слот: ")
        + bold(str(settings.extra_device_price_rub))
        + plain(" ₽, скидка 5% от 3 шт., 10% от 5, 15% от 10."),
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=_buy_qty_keyboard(ctx, can_buy),
        settings=settings,
    )
    await cq.answer()


@router.callback_query(F.data.startswith("dev:buyqty:"))
async def cb_dev_buy_qty_confirm(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    try:
        qty = int(parts[2])
        ctx = parts[3] if len(parts) > 3 else CTX_MAIN
    except (IndexError, ValueError):
        await cq.answer("Ошибка", show_alert=True)
        return
    settings = get_settings()
    sub = await get_active_subscription(session, db_user.id)
    if not sub:
        await cq.answer("Нет подписки.", show_alert=True)
        return
    can_buy = slots_available_to_buy(int(sub.devices_count), MAX_DEVICES)
    if qty < 1 or qty > can_buy:
        await cq.answer("Недопустимое количество.", show_alert=True)
        return
    total, unit, disc = price_for_extra_device_slots(settings, qty)
    token = secrets.token_urlsafe(10)
    cap = join_lines(
        "➕ " + bold("Подтверждение"),
        "",
        plain("Слотов: ") + bold(str(qty)),
        plain("К оплате: ") + bold(str(total)) + plain(" ₽"),
    )
    if disc > 0:
        cap = join_lines(
            cap,
            plain("Скидка: ") + bold(str(disc)) + plain("% (база ") + bold(str(unit)) + plain(" ₽/слот)"),
        )
    cap = join_lines(cap, "", plain("Баланс: ") + bold(str(db_user.balance)) + plain(" ₽"))
    kb = (
        InlineKeyboardBuilder()
        .row(
            InlineKeyboardButton(
                text="✅ Купить",
                callback_data=f"dev:buyok:{qty}:{token}:{ctx}",
                style="success",
            )
        )
        .row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"dev:buy:{ctx}", style="danger"))
        .as_markup()
    )
    await answer_callback_with_photo_screen(cq, caption=cap, reply_markup=kb, settings=settings)
    await cq.answer()


@router.callback_query(F.data.startswith("dev:buyok:"))
async def cb_dev_buy_ok(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    try:
        qty = int(parts[2])
        token = parts[3]
        ctx = parts[4] if len(parts) > 4 else CTX_MAIN
    except (IndexError, ValueError):
        await cq.answer("Ошибка", show_alert=True)
        return
    settings = get_settings()
    ok, msg = await add_paid_device_slots(
        session,
        user=db_user,
        settings=settings,
        quantity=qty,
        idempotency_key=f"devslots:{db_user.id}:{token}",
    )
    if not ok:
        await cq.answer(strip_for_popup_alert(msg), show_alert=True)
        return
    await session.commit()
    await cq.answer("Готово")
    await _open_devices_screen(cq, session, db_user, ctx=ctx)


@router.callback_query(F.data.startswith("dev:rw:"))
async def cb_dev_rw_pick(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    try:
        idx = int(parts[2])
        ctx = parts[3] if len(parts) > 3 else CTX_MAIN
    except (IndexError, ValueError):
        await cq.answer("Ошибка", show_alert=True)
        return

    settings = get_settings()
    _uinf, devices, err = await fetch_panel_hwid_context(db_user, settings)
    if err or not devices:
        await cq.answer("Список устройств недоступен", show_alert=True)
        return
    if idx < 0 or idx >= len(devices):
        await cq.answer("Устройство не найдено", show_alert=True)
        return

    sub = await get_active_subscription(session, db_user.id)
    if not sub:
        await cq.answer("Нет активной подписки.", show_alert=True)
        return

    can_remove_paid_slot = int(sub.devices_count) > MIN_DEVICES

    d = devices[idx]
    hwid = str(d.get("hwid") or "")
    plat = esc(str(d.get("platform") or "—"))
    osv = esc(str(d.get("osVersion") or "—"))
    model = esc(str(d.get("deviceModel") or "—"))
    agent = esc(str(d.get("userAgent") or "—"))
    created = esc(format_rw_device_datetime_local(str(d.get("createdAt") or "")))
    updated = esc(format_rw_device_datetime_local(str(d.get("updatedAt") or "")))

    cap = join_lines(
        "🖥 " + bold("Устройство ") + plain(f"#{idx + 1}"),
        "",
        plain("HWID: ") + code(hwid),
        plain("Платформа: ") + plat,
        plain("Версия ОС: ") + osv,
        plain("Модель: ") + model,
        plain("Агент: ") + agent,
        plain("Подключен в первые: ") + created,
        plain("Обновлён: ") + updated,
    )

    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="Отвязать устройство",
            callback_data=f"dev:unlk:{idx}:{ctx}",
            style="danger",
        ),
    )
    if can_remove_paid_slot:
        b.row(
            InlineKeyboardButton(
                text="Удалить слот",
                callback_data=f"dev:unlsask:{idx}:{ctx}",
                style="danger",
            ),
        )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=_list_callback(ctx), style="danger"))
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data.startswith("dev:unlsask:"))
async def cb_dev_rw_unlink_slot_confirm(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    try:
        idx = int(parts[2])
        ctx = parts[3] if len(parts) > 3 else CTX_MAIN
    except (IndexError, ValueError):
        await cq.answer("Ошибка", show_alert=True)
        return
    sub = await get_active_subscription(session, db_user.id)
    if not sub or int(sub.devices_count) <= MIN_DEVICES:
        await cq.answer(
            "Удалить слот нельзя: в подписке минимум два устройства.",
            show_alert=True,
        )
        return
    next_slots = int(sub.devices_count) - 1
    cap = join_lines(
        "🖥 " + bold("Удаление слота"),
        "",
        bold("Удалить слот с подписки?"),
        plain("Устройство на панели не отвязывается автоматически."),
        plain("После удаления слотов в подписке останется: ") + bold(str(next_slots)) + plain("."),
    )
    kb = (
        InlineKeyboardBuilder()
        .row(
            InlineKeyboardButton(
                text="✅ Да, удалить слот",
                callback_data=f"dev:unlslot:{idx}:{ctx}",
                style="danger",
            )
        )
        .row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"dev:rw:{idx}:{ctx}", style="danger"))
        .as_markup()
    )
    await answer_callback_with_photo_screen(cq, caption=cap, reply_markup=kb, settings=get_settings())


@router.callback_query(F.data.startswith("dev:unlslot:"))
async def cb_dev_rw_unlink_slot(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    try:
        idx = int(parts[2])
        ctx = parts[3] if len(parts) > 3 else CTX_MAIN
    except (IndexError, ValueError):
        await cq.answer("Ошибка", show_alert=True)
        return
    settings = get_settings()
    _uinf, devices, err = await fetch_panel_hwid_context(db_user, settings)
    if err:
        await cq.answer(strip_for_popup_alert(esc(err)), show_alert=True)
        return
    if idx < 0 or idx >= len(devices):
        await cq.answer("Устройство не найдено", show_alert=True)
        return
    hwid = str(devices[idx].get("hwid") or "")
    ok, msg = await remove_hwid_device_from_panel(
        session,
        user=db_user,
        hwid=hwid,
        settings=settings,
    )
    if not ok:
        await cq.answer(strip_for_popup_alert(msg), show_alert=True)
        return
    await session.commit()
    await cq.answer("Слот удалён")
    await _open_devices_screen(cq, session, db_user, ctx=ctx, is_bot_admin=is_bot_admin)


@router.callback_query(F.data.startswith("dev:unlk:"))
async def cb_dev_rw_unlink(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    try:
        idx = int(parts[2])
        ctx = parts[3] if len(parts) > 3 else CTX_MAIN
    except (IndexError, ValueError):
        await cq.answer("Ошибка", show_alert=True)
        return
    settings = get_settings()
    _uinf, devices, err = await fetch_panel_hwid_context(db_user, settings)
    if err:
        await cq.answer(strip_for_popup_alert(esc(err)), show_alert=True)
        return
    if idx < 0 or idx >= len(devices):
        await cq.answer("Устройство не найдено", show_alert=True)
        return
    hwid = str(devices[idx].get("hwid") or "")
    ok, msg = await unlink_hwid_device_keep_slots(
        session,
        user=db_user,
        hwid=hwid,
        settings=settings,
    )
    if not ok:
        await cq.answer(strip_for_popup_alert(msg), show_alert=True)
        return
    await session.commit()
    await cq.answer("Отвязано")
    await _open_devices_screen(cq, session, db_user, ctx=ctx, is_bot_admin=is_bot_admin)
