"""Главное меню и колбэки разделов."""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, User as TgUser
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user, support_telegram_url
from bot.keyboards.inline import main_menu_keyboard, submenu_back_keyboard
from shared.config import get_settings
from shared.integrations.remnawave import RemnaWaveError
from shared.models.user import User
from shared.services.trial_service import activate_trial, has_active_subscription, trial_eligible

logger = logging.getLogger(__name__)

router = Router(name="menu")


async def build_main_menu_kb(session: AsyncSession, user: User) -> tuple[bool, object]:
    settings = get_settings()
    has_act = await has_active_subscription(session, user.id)
    show_trial = trial_eligible(user, has_act)
    kb = main_menu_keyboard(
        show_trial=show_trial,
        support_url=support_telegram_url(settings.support_username),
    )
    return show_trial, kb


def main_menu_welcome_text(user: User) -> str:
    name = user.first_name or "друг"
    return (
        f"👋 Привет, <b>{html.escape(name)}</b>!\n\n"
        "Выберите раздел в меню ниже."
    )


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    state: FSMContext,
) -> None:
    await state.clear()
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    _, kb = await build_main_menu_kb(session, db_user)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(main_menu_welcome_text(db_user), reply_markup=kb)


@router.callback_query(F.data == "trial:activate")
async def cb_trial_activate(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    tg_user: TgUser | None,
) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    tg = tg_user or cq.from_user
    if tg is None:
        await cq.answer("Не удалось определить пользователя.", show_alert=True)
        return
    try:
        _sub, sub_url = await activate_trial(
            session,
            user=db_user,
            tg_user=tg,
            settings=settings,
        )
    except ValueError as e:
        await cq.answer(str(e), show_alert=True)
        return
    except RemnaWaveError:
        logger.exception("Remnawave trial failed")
        await cq.answer(
            "Не удалось выдать триал. Попробуйте позже или напишите в поддержку.",
            show_alert=True,
        )
        return
    except Exception:
        logger.exception("unexpected trial error")
        await cq.answer("Что-то пошло не так. Попробуйте позже.", show_alert=True)
        return

    from shared.services.admin_notify import notify_admin

    await notify_admin(
        settings,
        title="🎁 <b>Активирован триал</b>",
        lines=[
            f"Срок: <b>{settings.trial_duration_days}</b> дн., трафик: <b>{settings.trial_traffic_gb}</b> ГБ",
        ],
        event_type="trial_activate",
        subject_user=db_user,
        session=session,
    )

    await cq.answer()
    safe_url = html.escape(sub_url)
    _, kb = await build_main_menu_kb(session, db_user)
    text = (
        f"🎉 <b>Триал активирован!</b>\n"
        f"Срок: {settings.trial_duration_days} дн., трафик: {settings.trial_traffic_gb} ГБ.\n\n"
        f"Ссылка подписки (добавьте в приложение):\n<code>{safe_url}</code>\n\n"
        "Ниже — главное меню."
    )
    if cq.message:
        await cq.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "menu:instructions")
async def cb_instructions(cq: CallbackQuery, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    await cq.answer()
    b = InlineKeyboardBuilder()
    if settings.instruction_android_url:
        b.row(
            InlineKeyboardButton(
                text="🤖 Android",
                url=settings.instruction_android_url,
            )
        )
    if settings.instruction_ios_url:
        b.row(
            InlineKeyboardButton(
                text="🍎 iOS",
                url=settings.instruction_ios_url,
            )
        )
    if settings.instruction_windows_url:
        b.row(
            InlineKeyboardButton(
                text="🪟 Windows",
                url=settings.instruction_windows_url,
            )
        )
    if settings.instruction_macos_url:
        b.row(
            InlineKeyboardButton(
                text="💻 macOS",
                url=settings.instruction_macos_url,
            )
        )
    b.row(InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:main"))

    text = (
        "📖 <b>Инструкции по подключению</b>\n\n"
        "Выберите вашу платформу. Откроется статья с шагами подключения VPN.\n"
    )
    if not (
        settings.instruction_android_url
        or settings.instruction_ios_url
        or settings.instruction_windows_url
        or settings.instruction_macos_url
    ):
        text += (
            "\n⚠️ Ссылки пока не настроены. "
            "Укажите переменные INSTRUCTION_*_URL в .env."
        )
    if cq.message:
        await cq.message.edit_text(text, reply_markup=b.as_markup())


@router.callback_query(F.data == "menu:support")
async def cb_support(cq: CallbackQuery, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    await cq.answer(
        "Укажите SUPPORT_USERNAME в настройках бота или найдите контакт в канале.",
        show_alert=True,
    )


@router.callback_query(F.data == "menu:about")
async def cb_about(cq: CallbackQuery, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(
            "ℹ️ <b>О сервисе</b>\n\n"
            "VPN-доступ через панель Remnawave. По вопросам — поддержка.",
            reply_markup=submenu_back_keyboard(),
        )
