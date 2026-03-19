"""Реферальная программа: статистика, список приглашённых, ссылка (MarkdownV2)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.utils.screen_photo import answer_callback_with_photo_screen
from shared.config import get_settings
from shared.md2 import bold, code, esc, italic, join_lines
from shared.models.user import User
from shared.services.referral_service import (
    count_invited_users,
    list_invited_users,
    sum_referrer_bonus_days,
    sum_referrer_bonus_rub,
)

router = Router(name="referrals")


def _referrals_main_body(
    *,
    db_user: User,
    settings,
    invited: int,
    earned_rub,
    earned_days: int,
) -> str:
    bonus_rub = settings.referral_inviter_bonus_rub
    bonus_days = settings.referral_inviter_bonus_days
    cond_lines: list[str] = []
    if bonus_rub > 0:
        cond_lines.append(f"• {bold(str(bonus_rub))} ₽ за первую платную покупку друга")
    if bonus_days > 0:
        cond_lines.append(f"• {bold(str(bonus_days))} дн. к вашей подписке (если активна)")
    if not cond_lines:
        cond_lines.append("• Условия: первая " + bold("платная") + " покупка приглашённого")

    if settings.bot_username:
        uname = settings.bot_username.lstrip("@")
        link_u = f"https://t.me/{uname}?start=ref_{db_user.referral_code}"
        link_line = join_lines("🔗 Пригласить:", code(link_u))
    else:
        link_line = join_lines(
            f"Код: {code(db_user.referral_code)}",
            italic("Задайте BOT_USERNAME для готовой ссылки."),
        )

    cond_block = "\n".join(cond_lines)
    return join_lines(
        "👥 " + bold("Рефералы"),
        "",
        f"Приглашено людей: {bold(str(invited))}",
        f"Получено дней (бонусы): {bold(str(earned_days))}",
        f"Получено денег (бонусы): {bold(str(earned_rub))} ₽",
        "",
        bold("Как это работает"),
        cond_block,
        "",
        link_line,
    )


def _referrals_main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📋 Список приглашённых", callback_data="ref:list"))
    b.row(InlineKeyboardButton(text="⬅️ В профиль", callback_data="menu:main"))
    return b.as_markup()


@router.callback_query(F.data == "menu:referrals")
async def cb_referrals(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    invited = await count_invited_users(session, db_user.id)
    earned = await sum_referrer_bonus_rub(session, db_user.id)
    days_sum = await sum_referrer_bonus_days(session, db_user.id)
    body = _referrals_main_body(
        db_user=db_user,
        settings=settings,
        invited=invited,
        earned_rub=earned,
        earned_days=days_sum,
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=body,
        reply_markup=_referrals_main_kb(),
        settings=settings,
    )


@router.callback_query(F.data == "ref:list")
async def cb_ref_list(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    users = await list_invited_users(session, db_user.id, limit=25)
    if not users:
        lines = ["Пока никого не пригласили."]
    else:
        lines = []
        for u in users:
            un = f"@{esc(u.username)}" if u.username else "без username"
            lines.append(f"• {code(str(u.telegram_id))} {un}")
    body = join_lines("📋 " + bold("Приглашённые"), "", "\n".join(lines))
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ К рефералам", callback_data="menu:referrals"))
    await answer_callback_with_photo_screen(
        cq,
        caption=body,
        reply_markup=b.as_markup(),
        settings=settings,
    )
