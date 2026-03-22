"""Устройства: список из Remnawave HWID API, отвязка, платный слот (MarkdownV2)."""

from __future__ import annotations

from aiogram import F, Router
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
from shared.integrations.rw_traffic import extract_connected_devices_from_rw_user, rw_hwid_device_max
from shared.md2 import bold, code, esc, join_lines, plain
from shared.models.user import User
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin
from shared.services.subscription_service import (
    MAX_DEVICES,
    MIN_DEVICES,
    add_paid_device_slot,
    get_active_subscription,
    remove_hwid_device_from_panel,
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

    if uinf is not None:
        max_panel = rw_hwid_device_max(uinf)
        if max_panel is None:
            max_part = bold("∞")
        else:
            max_part = bold(str(max_panel))
    else:
        max_part = bold(str(sub.devices_count))

    lines = join_lines(
        "🖥 " + bold("Устройства"),
        "",
        plain("Слотов в подписке (бот): ")
        + bold(str(sub.devices_count))
        + plain(f" (мин. {MIN_DEVICES}, макс. {MAX_DEVICES})"),
        plain("В панели: ")
        + bold(str(used))
        + plain(" / ")
        + max_part
        + plain(" (подключено / лимит)"),
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
) -> None:
    settings = get_settings()
    text, kb = await _render_devices(session, db_user, ctx=ctx)
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
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    await _open_devices_screen(cq, session, db_user, ctx=CTX_MAIN)


@router.callback_query(F.data == "sub:devices")
async def cb_devices_from_sub(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    await _open_devices_screen(cq, session, db_user, ctx=CTX_SUB)


@router.callback_query(F.data.startswith("dev:list:"))
async def cb_dev_list(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    ctx = parts[2] if len(parts) > 2 else CTX_MAIN
    await _open_devices_screen(cq, session, db_user, ctx=ctx)
    await cq.answer()


@router.callback_query(F.data.startswith("dev:add:"))
async def cb_dev_add(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    ctx = parts[2] if len(parts) > 2 else CTX_MAIN
    settings = get_settings()
    ok, msg = await add_paid_device_slot(session, user=db_user, settings=settings)
    if ok:
        await notify_admin(
            settings,
            title="🖥 " + bold("Куплен слот устройства"),
            lines=[f"Списано: {bold(str(settings.extra_device_price_rub))} ₽"],
            event_type="extra_device_purchase",
            topic=AdminLogTopic.DEVICES,
            subject_user=db_user,
            session=session,
        )
    if not ok:
        await cq.answer(msg, show_alert=True)
        return
    text, kb = await _render_devices(session, db_user, ctx=ctx)
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
        InlineKeyboardButton(text="✅ Отвязать", callback_data=f"dev:unl:{idx}:{ctx}"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=_list_callback(ctx)),
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data.startswith("dev:unl:"))
async def cb_dev_rw_unlink(
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
    if err or idx < 0 or idx >= len(devices):
        await cq.answer("Устройство не найдено", show_alert=True)
        return
    hwid = str(devices[idx].get("hwid") or "")

    ok, msg = await remove_hwid_device_from_panel(session, user=db_user, hwid=hwid, settings=settings)
    if not ok:
        await cq.answer(msg, show_alert=True)
        return

    text, kb = await _render_devices(session, db_user, ctx=ctx)
    cap = join_lines(text, "", msg)
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=kb,
        settings=settings,
    )
