"""Реферальная программа: ссылка, условия, статистика."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user
from bot.keyboards.inline import submenu_back_keyboard
from shared.config import get_settings
from shared.models.user import User
from shared.services.referral_service import count_invited_users, sum_referrer_bonus_rub

router = Router(name="referrals")


@router.callback_query(F.data == "menu:referrals")
async def cb_referrals(cq: CallbackQuery, session: AsyncSession, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    await cq.answer()

    invited = await count_invited_users(session, db_user.id)
    earned = await sum_referrer_bonus_rub(session, db_user.id)

    bonus_rub = settings.referral_inviter_bonus_rub
    bonus_days = settings.referral_inviter_bonus_days
    cond_lines: list[str] = []
    if bonus_rub > 0:
        cond_lines.append(f"• <b>{bonus_rub}</b> ₽ на баланс пригласившему")
    if bonus_days > 0:
        cond_lines.append(f"• <b>{bonus_days}</b> дн. к подписке пригласившего (если она активна)")
    if not cond_lines:
        cond_lines.append("• Бонусы не настроены (задайте <code>REFERRAL_INVITER_BONUS_RUB</code> / <code>DAYS</code> в .env)")

    cond_block = "\n".join(cond_lines)

    if settings.bot_username:
        uname = settings.bot_username.lstrip("@")
        link = f"https://t.me/{uname}?start=ref_{db_user.referral_code}"
        link_line = f"Ваша ссылка:\n<code>{html.escape(link)}</code>"
    else:
        link_line = (
            f"Ваш код: <code>{html.escape(db_user.referral_code)}</code>\n"
            "Укажите <code>BOT_USERNAME</code> в .env для готовой ссылки."
        )

    body = (
        "👥 <b>Реферальная программа</b>\n\n"
        f"{link_line}\n\n"
        "<b>Условия (первая платная покупка друга):</b>\n"
        f"{cond_block}\n\n"
        f"Приглашено по ссылке: <b>{invited}</b>\n"
        f"Начислено бонусами: <b>{earned}</b> ₽"
    )
    if cq.message:
        await cq.message.edit_text(body, reply_markup=submenu_back_keyboard())
