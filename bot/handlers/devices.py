"""Устройства: список по нажатию, отвязка, платный слот (MarkdownV2)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.device import Device

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.utils.screen_photo import answer_callback_with_photo_screen
from shared.config import get_settings
from shared.md2 import bold, code, esc, join_lines, plain
from shared.models.user import User
from shared.services.admin_notify import notify_admin
from shared.services.subscription_service import (
    MAX_DEVICES,
    MIN_DEVICES,
    add_paid_device_slot,
    get_active_subscription,
    list_user_devices,
    remove_device_slot,
)

router = Router(name="devices")

CTX_MAIN = "main"
CTX_SUB = "sub"


def _devices_back_cb(ctx: str) -> str:
    return "menu:sub_main" if ctx == CTX_SUB else "menu:main"


def _device_button_label(d) -> str:
    base = (d.name or "Устройство")[:20]
    if d.remnawave_client_id:
        tail = d.remnawave_client_id.strip()
        short = tail[-12:] if len(tail) > 12 else tail
        label = f"{base} · {short}"
    else:
        label = f"{base} · id{d.id}"
    return label[:60]


def _devices_kb(devices: list, slots: int, price_label: str, ctx: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for d in devices:
        b.row(
            InlineKeyboardButton(
                text=_device_button_label(d),
                callback_data=f"dev:pick:{d.id}:{ctx}",
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

    devices = await list_user_devices(session, sub.id)
    used = len(devices)
    connected_lines: list[str] = []
    for d in devices:
        line = (
            plain("• ")
            + esc(d.name or "Устройство")
            + plain(" · id ")
            + code(str(d.id))
        )
        if d.remnawave_client_id:
            line += plain(" · rw ") + code(d.remnawave_client_id)
        connected_lines.append(line)
    connected_block = "\n".join(connected_lines) if connected_lines else esc("• нет")

    lines = join_lines(
        "🖥 " + bold("Устройства"),
        "",
        plain(f"Слотов в подписке: ")
        + bold(str(sub.devices_count))
        + plain(f" (мин. {MIN_DEVICES}, макс. {MAX_DEVICES})"),
        plain("Занято слотов: ") + bold(str(used)) + plain("/") + bold(str(sub.devices_count)),
        "",
        plain("Подключенные устройства:"),
        connected_block,
        "",
        plain("Нажмите устройство, чтобы ") + bold("отвязать") + plain(" его."),
    )
    price = str(settings.extra_device_price_rub)
    return lines, _devices_kb(devices, sub.devices_count, price, ctx)


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


@router.callback_query(F.data.startswith("dev:pick:"))
async def cb_dev_pick(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    try:
        did = int(parts[2])
        ctx = parts[3] if len(parts) > 3 else CTX_MAIN
    except (IndexError, ValueError):
        await cq.answer("Ошибка", show_alert=True)
        return
    r = await session.execute(select(Device).where(Device.id == did, Device.user_id == db_user.id))
    dev = r.scalar_one_or_none()
    if dev is None:
        await cq.answer("Устройство не найдено.", show_alert=True)
        return
    nm = esc(dev.name or "—")
    settings = get_settings()
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Отвязать", callback_data=f"dev:do:{did}:{ctx}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=_devices_back_cb(ctx)),
    )
    cap = join_lines(
        "🖥 " + bold("Устройство"),
        "",
        nm,
        "",
        plain("Отвязать это устройство? Слот в панели уменьшится (минимум 2 на аккаунт)."),
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=b.as_markup(),
        settings=settings,
    )


@router.callback_query(F.data.startswith("dev:do:"))
async def cb_dev_do(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    parts = cq.data.split(":")
    try:
        did = int(parts[2])
        ctx = parts[3] if len(parts) > 3 else CTX_MAIN
    except (IndexError, ValueError):
        await cq.answer("Ошибка", show_alert=True)
        return
    settings = get_settings()
    ok, msg = await remove_device_slot(session, user=db_user, device_id=did, settings=settings)
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
