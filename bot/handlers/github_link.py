"""Привязка/отвязка GitHub-профиля в Telegram-боте."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message, User as TgUser
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user, support_telegram_url
from bot.keyboards.profile_kb import profile_main_keyboard
from bot.states.github_link import GithubLinkStates
from bot.ui.profile_text import profile_caption
from bot.utils.screen_photo import answer_callback_with_photo_screen, delete_message_safe
from shared.config import get_settings
from shared.md2 import bold, code, join_lines, plain
from shared.models.user import User
from shared.services.subscription_service import get_active_subscription
from shared.services.trial_service import trial_eligible

router = Router(name="github_link")

_GITHUB_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_GITHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<username>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/?$",
    re.IGNORECASE,
)


def _github_link_keyboard(*, has_linked: bool) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    if has_linked:
        b.row(InlineKeyboardButton(text="🗑 Отвязать GitHub", callback_data="github:unlink"))
    b.row(InlineKeyboardButton(text="⬅️ В профиль", callback_data="github:cancel"))
    return b


def _parse_github_username(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        text = text[1:].strip()
    m = _GITHUB_URL_RE.match(text)
    if m:
        text = m.group("username")
    if not _GITHUB_RE.match(text):
        return None
    if "--" in text:
        return None
    return text


@router.callback_query(F.data == "menu:github")
async def cb_github_open(
    cq: CallbackQuery, db_user: User | None, state: FSMContext, is_bot_admin: bool = False
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    if not is_bot_admin:
        await cq.answer("Раздел доступен только администраторам.", show_alert=True)
        return
    assert db_user is not None
    await state.set_state(GithubLinkStates.waiting_username)
    settings = get_settings()
    current = db_user.github_username or "не привязан"
    caption = join_lines(
        "🔗 " + bold("Привязка GitHub"),
        "",
        plain("Текущий GitHub: ") + bold(current),
        "",
        plain("Отправьте username или ссылку вида:"),
        code("https://github.com/yourname"),
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=caption,
        reply_markup=_github_link_keyboard(has_linked=bool(db_user.github_username)).as_markup(),
        settings=settings,
    )


@router.callback_query(F.data == "github:cancel")
async def cb_github_cancel(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
    tg_user: TgUser | None,
    is_bot_admin: bool = False,
) -> None:
    await state.clear()
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    tg = tg_user or cq.from_user
    if tg is None:
        await cq.answer("Не удалось определить пользователя.", show_alert=True)
        return
    settings = get_settings()
    has_act = await get_active_subscription(session, db_user.id) is not None
    show_trial = bool(settings.trial_enabled and trial_eligible(db_user, has_act))
    cap = profile_caption(db_user, tg, is_admin=is_bot_admin)
    kb = profile_main_keyboard(
        show_trial=show_trial,
        support_url=support_telegram_url(settings.support_username),
        is_admin=is_bot_admin,
    )
    await cq.answer()
    await answer_callback_with_photo_screen(cq, caption=cap, reply_markup=kb, settings=settings)


@router.callback_query(F.data == "github:unlink")
async def cb_github_unlink(cq: CallbackQuery, db_user: User | None, state: FSMContext) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    db_user.github_id = None
    db_user.github_username = None
    db_user.github_profile_url = None
    await state.set_state(GithubLinkStates.waiting_username)
    settings = get_settings()
    await cq.answer("GitHub отвязан")
    await answer_callback_with_photo_screen(
        cq,
        caption=join_lines(
            "🔗 " + bold("Привязка GitHub"),
            "",
            plain("Связь удалена. Отправьте новый username или нажмите «В профиль»."),
        ),
        reply_markup=_github_link_keyboard(has_linked=False).as_markup(),
        settings=settings,
    )


@router.message(GithubLinkStates.waiting_username, F.text)
async def msg_github_username(
    message: Message,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
) -> None:
    if await reject_if_blocked(message, db_user) or db_user is None:
        await state.clear()
        return
    gh_username = _parse_github_username(message.text or "")
    if gh_username is None:
        await message.answer(
            join_lines(
                "❌ " + bold("Неверный GitHub username"),
                plain("Пример: ") + code("torvalds"),
                plain("или ") + code("https://github.com/torvalds"),
            )
        )
        return

    taken = (
        await session.execute(
            select(User.id)
            .where(
                User.id != db_user.id,
                func.lower(User.github_username) == gh_username.lower(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if taken is not None:
        await message.answer("Этот GitHub уже привязан к другому аккаунту.")
        return

    db_user.github_id = None
    db_user.github_username = gh_username
    db_user.github_profile_url = f"https://github.com/{gh_username}"

    await state.clear()
    await delete_message_safe(message)
    await message.answer(
        join_lines(
            "✅ " + bold("GitHub привязан"),
            plain("Профили Telegram и GitHub теперь связаны в одном аккаунте данных."),
            plain("GitHub: ") + code(db_user.github_profile_url),
        )
    )
