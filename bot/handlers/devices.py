"""Устройства: список из Remnawave HWID API, отвязка, платный слот (MarkdownV2)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.utils.screen_photo import answer_callback_with_photo_screen
from shared.config import get_settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.integrations.rw_hwid_devices import (
    format_rw_device_datetime_local,
    hwid_device_title,
    normalize_hwid_devices_list,
)
from shared.integrations.rw_traffic import extract_connected_devices_from_rw_user, is_rw_hwid_devices_unlimited
from shared.md2 import bold, code, esc, join_lines, plain, strip_for_popup_alert
from shared.models.user import User
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin
from shared.services.subscription_service import (
    MAX_DEVICES,
    add_paid_device_slot,
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


async def _fetch_panel_hwid_context(
    user: User, settings
) -> tuple[dict | None, list[dict], str | None]:
    """
    get_user (для лимита слотов) + список HWID.
    uinf может быть не None даже при ошибке списка HWID.
    """
    if user.remnawave_uuid is None:
        return None, [], "Remnawave не привязан к профилю."
    rw = RemnaWaveClient(settings)
    uinf: dict | None = None
    try:
        uinf = await rw.get_user(str(user.remnawave_uuid))
    except RemnaWaveError:
        uinf = None
    try:
        raw = await rw.get_user_hwid_devices(str(user.remnawave_uuid))
    except RemnaWaveError as e:
        return uinf, [], str(e)
    return uinf, normalize_hwid_devices_list(raw), None


def _devices_kb(
    devices: list[dict],
    *,
    slots: int,
    price_label: str,
    ctx: str,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, d in enumerate(devices):
        b.row(
            InlineKeyboardButton(
                text=hwid_device_title(d, i + 1),
                callback_data=f"dev:rw:{i}:{ctx}",
            )
        )
    if slots < MAX_DEVICES:
        b.row(
            InlineKeyboardButton(
                text=f"➕ Добавить слот ({price_label} ₽)",
                callback_data=f"dev:add:{ctx}",
            )
        )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=_devices_back_cb(ctx)))
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
            .row(InlineKeyboardButton(text="⬅️ Назад", callback_data=_devices_back_cb(ctx)))
            .as_markup()
        )
        return join_lines("🖥 " + bold("Устройства"), "", plain("Сначала оформите подписку или триал.")), kb

    uinf, devices, err = await _fetch_panel_hwid_context(user, settings)

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
            .row(InlineKeyboardButton(text="🔄 Обновить", callback_data=_list_callback(ctx)))
            .row(InlineKeyboardButton(text="⬅️ Назад", callback_data=_devices_back_cb(ctx)))
            .as_markup()
        )
        return cap, kb

    # Как на экране «Подписка»: число из HWID API; если список пуст, но get_user есть — запасной счётчик
    used = len(devices)
    if uinf is not None and used == 0:
        n_alt = extract_connected_devices_from_rw_user(uinf)
        if n_alt is not None:
            used = n_alt

    denom_unlimited = is_bot_admin or (uinf is not None and is_rw_hwid_devices_unlimited(uinf))
    denom = bold("∞") if denom_unlimited else bold(str(sub.devices_count))
    slots_line = plain("📟 Слоты: ") + bold(str(used)) + plain(" / ") + denom
    bound_line = plain("📱 Привязанные устройства: ") + bold(str(used))

    lines = join_lines(
        "🖥 " + bold("Устройства"),
        "",
        slots_line,
        bound_line,
        "",
        plain("Нажмите устройство, чтобы посмотреть детали и ") + bold("отвязать") + plain("."),
    )
    price = str(settings.extra_device_price_rub)
    return lines, _devices_kb(devices, slots=sub.devices_count, price_label=price, ctx=ctx)


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


@router.callback_query(F.data.startswith("dev:add:"))
async def cb_dev_add(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    ctx = parts[2] if len(parts) > 2 else CTX_MAIN
    settings = get_settings()
    token = secrets.token_urlsafe(8)
    await state.update_data(
        dev_add_confirm_token=token,
        dev_add_confirm_ctx=ctx,
        dev_add_confirm_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"dev:addconfirm:{ctx}:{token}"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"dev:list:{ctx}"))
    cap = join_lines(
        "🧾 " + bold("Подтверждение"),
        "",
        plain("Добавить 1 слот устройства за ")
        + bold(str(settings.extra_device_price_rub))
        + plain(" ₽?"),
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data.startswith("dev:addconfirm:"))
async def cb_dev_add_confirm(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
    is_bot_admin: bool = False,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    if len(parts) < 4:
        await cq.answer("Ошибка подтверждения", show_alert=True)
        return
    ctx = parts[2]
    token = parts[3]
    data = await state.get_data()
    valid_token = data.get("dev_add_confirm_token")
    valid_ctx = data.get("dev_add_confirm_ctx")
    exp_raw = data.get("dev_add_confirm_expires_at")
    if not isinstance(valid_token, str) or token != valid_token or valid_ctx != ctx:
        await cq.answer("Подтверждение уже использовано или устарело.", show_alert=True)
        return
    if isinstance(exp_raw, str):
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(exp_raw):
                await cq.answer("Время подтверждения истекло.", show_alert=True)
                return
        except ValueError:
            pass
    await state.update_data(
        dev_add_confirm_token=None,
        dev_add_confirm_ctx=None,
        dev_add_confirm_expires_at=None,
    )
    settings = get_settings()
    ok, msg = await add_paid_device_slot(
        session,
        user=db_user,
        settings=settings,
        idempotency_key=f"devadd:{db_user.id}:{token}",
    )
    if ok:
        await notify_admin(
            settings,
            title="🖥 " + bold("Куплен слот устройства"),
            lines=[
                plain("Списано: ")
                + bold(str(settings.extra_device_price_rub))
                + plain(" ₽"),
            ],
            event_type="extra_device_purchase",
            topic=AdminLogTopic.DEVICES,
            subject_user=db_user,
            session=session,
        )
    if not ok:
        await cq.answer(msg, show_alert=True)
        return
    text, kb = await _render_devices(session, db_user, ctx=ctx, is_bot_admin=is_bot_admin)
    cap = join_lines(text, "", msg)
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=kb,
        settings=settings,
    )


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
    _uinf, devices, err = await _fetch_panel_hwid_context(db_user, settings)
    if err or not devices:
        await cq.answer("Список устройств недоступен", show_alert=True)
        return
    if idx < 0 or idx >= len(devices):
        await cq.answer("Устройство не найдено", show_alert=True)
        return

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
        InlineKeyboardButton(text="📤 Только панель", callback_data=f"dev:unlk:{idx}:{ctx}"),
        InlineKeyboardButton(text="📤 Панель − слот", callback_data=f"dev:unls:{idx}:{ctx}"),
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=_list_callback(ctx)))
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=b.as_markup(),
        settings=settings,
    )


async def _cb_dev_rw_unlink_impl(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    *,
    ctx: str,
    idx: int,
    is_bot_admin: bool,
    decrease_slot: bool,
) -> None:
    settings = get_settings()
    _uinf, devices, err = await _fetch_panel_hwid_context(db_user, settings)
    if err or idx < 0 or idx >= len(devices):
        await cq.answer("Устройство не найдено", show_alert=True)
        return
    hwid = str(devices[idx].get("hwid") or "")

    if decrease_slot:
        ok, msg = await remove_hwid_device_from_panel(session, user=db_user, hwid=hwid, settings=settings)
    else:
        ok, msg = await unlink_hwid_device_keep_slots(session, user=db_user, hwid=hwid, settings=settings)
    if not ok:
        await cq.answer(strip_for_popup_alert(msg)[:200], show_alert=True)
        return

    text, kb = await _render_devices(session, db_user, ctx=ctx, is_bot_admin=is_bot_admin)
    cap = join_lines(text, "", msg)
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=kb,
        settings=settings,
    )


@router.callback_query(F.data.startswith("dev:unlk:"))
async def cb_dev_rw_unlink_keep_slots(
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
    await _cb_dev_rw_unlink_impl(
        cq, session, db_user, ctx=ctx, idx=idx, is_bot_admin=is_bot_admin, decrease_slot=False
    )


@router.callback_query(F.data.startswith("dev:unls:"))
async def cb_dev_rw_unlink_and_slot(
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
    await _cb_dev_rw_unlink_impl(
        cq, session, db_user, ctx=ctx, idx=idx, is_bot_admin=is_bot_admin, decrease_slot=True
    )
