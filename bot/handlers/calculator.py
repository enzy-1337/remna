"""Калькулятор «тариф vs pay-as-you-go» (фаза 8 мастер-плана)."""

from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.utils.screen_photo import answer_callback_with_photo_screen, safe_callback_answer
from shared.config import get_settings
from shared.md2 import bold, join_lines, plain
from shared.models.plan import Plan
from shared.models.user import User
from shared.services.billing_calculator import (
    compare_plan_vs_payg_estimate,
    estimate_payg_scenario_rub,
)
from shared.services.subscription_service import list_paid_plans

router = Router(name="calculator")

# calc:s:period_days:device_days:gb_steps:mobile_gb_steps:opt_flag
_SCENARIO_PREFIX = "calc:s:"
_COMPARE_PREFIX = "calc:c:"
_MENU = "calc:menu"


def _scenario_cb(period_days: int, device_days: int, gb: int, mob: int, opt: int) -> str:
    return f"{_SCENARIO_PREFIX}{period_days}:{device_days}:{gb}:{mob}:{opt}"


def _compare_cb(plan_id: int, period_days: int, device_days: int, gb: int, mob: int, opt: int) -> str:
    return f"{_COMPARE_PREFIX}{plan_id}:{period_days}:{device_days}:{gb}:{mob}:{opt}"


def _parse_ints(data: str, prefix: str, n: int) -> tuple[int, ...] | None:
    if not data.startswith(prefix):
        return None
    rest = data[len(prefix) :]
    parts = rest.split(":")
    if len(parts) != n:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _presets_keyboard() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="1×30 дн · 15 ГБ", callback_data=_scenario_cb(30, 30, 15, 0, 0)),
        InlineKeyboardButton(text="2×30 · 15 ГБ", callback_data=_scenario_cb(30, 60, 15, 0, 0)),
    )
    b.row(
        InlineKeyboardButton(text="1×7 дн · 5 ГБ", callback_data=_scenario_cb(7, 7, 5, 0, 0)),
        InlineKeyboardButton(text="1×30 · 15 ГБ + опт.", callback_data=_scenario_cb(30, 30, 15, 0, 1)),
    )
    b.row(
        InlineKeyboardButton(text="1×30 · 10 ГБ + 2 моб.", callback_data=_scenario_cb(30, 30, 10, 2, 0)),
    )
    b.row(InlineKeyboardButton(text="⬅️ В профиль", callback_data="menu:main"))
    return b


def _plans_keyboard(
    plans: list[Plan],
    *,
    period_days: int,
    device_days: int,
    gb: int,
    mob: int,
    opt: int,
) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for p in plans:
        label = f"{p.name[:28]} — {p.price_rub} ₽"
        if len(label) > 64:
            label = f"{p.name[:20]}… {p.price_rub}₽"
        b.row(InlineKeyboardButton(text=label, callback_data=_compare_cb(p.id, period_days, device_days, gb, mob, opt)))
    b.row(InlineKeyboardButton(text="↩️ Другой сценарий", callback_data=_MENU))
    b.row(InlineKeyboardButton(text="⬅️ В профиль", callback_data="menu:main"))
    return b


def _fmt_money(d: Decimal) -> str:
    return str(d.quantize(Decimal("0.01")))


def _scenario_caption(
    *,
    period_days: int,
    device_days: int,
    gb: int,
    mob: int,
    opt: bool,
    est: dict[str, Decimal],
) -> str:
    approx_devices = device_days // max(1, period_days)
    head = join_lines(
        "📊 " + bold("Сценарий"),
        "",
        plain(
            f"Период: {period_days} дн., усл. устройств: ~{approx_devices}, "
            f"шагов ГБ: {gb}, моб. шагов: {mob}, оптим. маршрут: {'да' if opt else 'нет'}."
        ),
        "",
        plain("Оценка pay-as-you-go:"),
        plain(f"• Устройства: {_fmt_money(est['device_rub'])} ₽"),
        plain(f"• Трафик (шаги ГБ): {_fmt_money(est['traffic_rub'])} ₽"),
        plain(f"• Моб. доплата: {_fmt_money(est['mobile_extra_rub'])} ₽"),
        plain(f"• Оптим. маршрут: {_fmt_money(est['optimized_extra_rub'])} ₽"),
        "",
        bold(f"Итого: {_fmt_money(est['total_rub'])} ₽"),
        "",
        plain("Выберите тариф ниже — покажем цену плана за тот же период."),
    )
    return head


def _append_no_plans_note(cap: str, plans: list[Plan]) -> str:
    if plans:
        return cap
    return cap + "\n\n" + plain("Активных платных тарифов в базе пока нет — сравнение недоступно.")


@router.callback_query(F.data.in_(("menu:calc", _MENU)))
async def cb_calc_menu(
    cq: CallbackQuery,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    settings = get_settings()
    if not settings.billing_v2_enabled:
        await safe_callback_answer(cq, "Калькулятор доступен при включённом гибридном биллинге.", show_alert=True)
        return
    cap = join_lines(
        "📊 " + bold("Калькулятор: пакет vs pay-as-you-go"),
        "",
        plain("Выберите типичный сценарий — мы оценим списания по балансу и сравним с ценой тарифа."),
        plain("Цифры ориентировочные; фактический расход зависит от трафика и устройств."),
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=_presets_keyboard().as_markup(),
        settings=settings,
    )


@router.callback_query(F.data.startswith(_SCENARIO_PREFIX))
async def cb_calc_scenario(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    settings = get_settings()
    if not settings.billing_v2_enabled:
        await safe_callback_answer(cq, "Калькулятор недоступен.", show_alert=True)
        return
    parsed = _parse_ints(cq.data or "", _SCENARIO_PREFIX, 5)
    if parsed is None:
        await safe_callback_answer(cq, "Некорректные данные.", show_alert=True)
        return
    period_days, device_days, gb, mob, opt_i = parsed
    if (
        period_days < 1
        or period_days > 366
        or device_days < 1
        or device_days > 20000
        or gb < 0
        or gb > 2000
        or mob < 0
        or mob > 500
        or opt_i not in (0, 1)
    ):
        await safe_callback_answer(cq, "Недопустимый сценарий.", show_alert=True)
        return
    est = estimate_payg_scenario_rub(
        settings,
        device_days=device_days,
        gb_steps=gb,
        mobile_gb_steps=mob,
        optimized_route=bool(opt_i),
    )
    plans = await list_paid_plans(session)
    cap = _append_no_plans_note(
        _scenario_caption(
            period_days=period_days,
            device_days=device_days,
            gb=gb,
            mob=mob,
            opt=bool(opt_i),
            est=est,
        ),
        plans,
    )
    if plans:
        kb = _plans_keyboard(
            plans,
            period_days=period_days,
            device_days=device_days,
            gb=gb,
            mob=mob,
            opt=opt_i,
        ).as_markup()
    else:
        nb = InlineKeyboardBuilder()
        nb.row(InlineKeyboardButton(text="↩️ Другой сценарий", callback_data=_MENU))
        nb.row(InlineKeyboardButton(text="⬅️ В профиль", callback_data="menu:main"))
        kb = nb.as_markup()
    await answer_callback_with_photo_screen(cq, caption=cap, reply_markup=kb, settings=settings)


@router.callback_query(F.data.startswith(_COMPARE_PREFIX))
async def cb_calc_compare(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    settings = get_settings()
    if not settings.billing_v2_enabled:
        await safe_callback_answer(cq, "Калькулятор недоступен.", show_alert=True)
        return
    parsed = _parse_ints(cq.data or "", _COMPARE_PREFIX, 6)
    if parsed is None:
        await safe_callback_answer(cq, "Некорректные данные.", show_alert=True)
        return
    plan_id, period_days, device_days, gb, mob, opt_i = parsed
    if (
        plan_id < 1
        or period_days < 1
        or period_days > 366
        or device_days < 1
        or device_days > 20000
        or gb < 0
        or gb > 2000
        or mob < 0
        or mob > 500
        or opt_i not in (0, 1)
    ):
        await safe_callback_answer(cq, "Недопустимый сценарий.", show_alert=True)
        return
    r = await session.execute(select(Plan).where(Plan.id == plan_id, Plan.is_active.is_(True)).limit(1))
    plan = r.scalar_one_or_none()
    if plan is None or plan.price_rub <= 0:
        await safe_callback_answer(cq, "Тариф не найден.", show_alert=True)
        return
    est = estimate_payg_scenario_rub(
        settings,
        device_days=device_days,
        gb_steps=gb,
        mobile_gb_steps=mob,
        optimized_route=bool(opt_i),
    )
    cmp_ = compare_plan_vs_payg_estimate(plan, period_days=period_days, payg_estimate=est)
    delta = cmp_["delta_rub"]
    if delta > 0:
        verdict = plain(f"По этому сценарию пакет дороже оценки pay-as-you-go на {_fmt_money(delta)} ₽.")
    elif delta < 0:
        verdict = plain(f"Пакет дешевле оценки pay-as-you-go на {_fmt_money(-delta)} ₽.")
    else:
        verdict = plain("Пакет и оценка совпадают по сумме.")
    cap = join_lines(
        _scenario_caption(
            period_days=period_days,
            device_days=device_days,
            gb=gb,
            mob=mob,
            opt=bool(opt_i),
            est=est,
        ),
        "",
        "📋 " + bold("Сравнение с тарифом"),
        plain(f"Тариф: {plan.name}"),
        plain(
            f"Цена плана за {period_days} дн. (пропорционально сроку плана): "
            f"{_fmt_money(cmp_['plan_rub'])} ₽"
        ),
        "",
        verdict,
    )
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="↩️ Другой сценарий", callback_data=_MENU))
    b.row(InlineKeyboardButton(text="⬅️ В профиль", callback_data="menu:main"))
    await answer_callback_with_photo_screen(cq, caption=cap, reply_markup=b.as_markup(), settings=settings)
