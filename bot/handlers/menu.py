"""Главный экран «Профиль», инструкции, вспомогательные колбэки."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, User as TgUser
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.common import reject_if_blocked, reject_if_no_user, support_telegram_url
from bot.keyboards.instructions_kb import build_instructions_markup
from bot.keyboards.profile_kb import profile_main_keyboard
from bot.ui.profile_text import profile_caption
from bot.utils.screen_photo import answer_callback_with_photo_screen
from shared.config import get_settings
from shared.integrations.remnawave import RemnaWaveError
from shared.models.plan import Plan
from shared.models.user import User
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin
from shared.services.subscription_service import get_active_subscription
from shared.md2 import bold, code, join_lines, link, plain
from shared.services.trial_service import activate_trial, trial_eligible

logger = logging.getLogger(__name__)

router = Router(name="menu")


def _service_info_keyboard(support_url: str | None) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    if support_url:
        b.row(InlineKeyboardButton(text="💬 Поддержка", url=support_url))
    else:
        b.row(InlineKeyboardButton(text="💬 Поддержка", callback_data="menu:support"))
    b.row(InlineKeyboardButton(text="⬅️ В профиль", callback_data="menu:main"))
    return b


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(
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
    # Кнопка покупки всегда доступна, если подписки нет.
    cap = profile_caption(db_user, tg)
    kb = profile_main_keyboard(
        has_active_sub=has_act,
        show_trial=show_trial,
        support_url=support_telegram_url(settings.support_username),
        is_admin=is_bot_admin,
    )
    await answer_callback_with_photo_screen(cq, caption=cap, reply_markup=kb, settings=settings)


@router.callback_query(F.data == "trial:activate")
async def cb_trial_activate(
    cq: CallbackQuery,
    session: AsyncSession,
    db_user: User | None,
    tg_user: TgUser | None,
    is_bot_admin: bool = False,
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

    await notify_admin(
        settings,
        title="🎁 " + bold("Активирован триал"),
        lines=[
            plain("Срок: ")
            + bold(str(settings.trial_duration_days))
            + plain(" дн., трафик: ")
            + bold(str(settings.trial_traffic_gb))
            + plain(" ГБ"),
        ],
        event_type="trial_activate",
        topic=AdminLogTopic.TRIALS,
        subject_user=db_user,
        session=session,
    )

    has_act = await get_active_subscription(session, db_user.id) is not None
    show_trial = bool(settings.trial_enabled and trial_eligible(db_user, has_act))
    # Кнопка покупки всегда доступна, если подписки нет.
    cap = join_lines(
        "🎉 " + bold("Триал активирован!"),
        "",
        plain(
            f"Срок: {settings.trial_duration_days} дн., трафик: {settings.trial_traffic_gb} ГБ."
        ),
        "",
        plain("Ссылка подписки:"),
        code(sub_url),
        "",
        profile_caption(db_user, tg),
    )
    kb = profile_main_keyboard(
        has_active_sub=has_act,
        show_trial=show_trial,
        support_url=support_telegram_url(settings.support_username),
        is_admin=is_bot_admin,
    )
    await answer_callback_with_photo_screen(cq, caption=cap, reply_markup=kb, settings=settings)


@router.callback_query(F.data == "menu:instructions")
async def cb_instructions(cq: CallbackQuery, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    kb = build_instructions_markup(
        settings,
        back_callback="menu:main",
        back_text="⬅️ В профиль",
    )
    text = join_lines(
        "📖 " + bold("Инструкции"),
        "",
        plain("Откройте статью на Telegra.ph для вашего устройства."),
    )
    if (
        not settings.instruction_telegraph_phone_url
        and not settings.instruction_telegraph_pc_url
        and not (
            settings.instruction_android_url
            or settings.instruction_ios_url
            or settings.instruction_windows_url
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


@router.callback_query(F.data == "menu:support")
async def cb_support(cq: CallbackQuery, db_user: User | None) -> None:
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    await cq.answer(
        "Укажите SUPPORT_USERNAME в .env — тогда кнопка поддержки станет ссылкой на Telegram.",
        show_alert=True,
    )


@router.callback_query(F.data.in_(("menu:info", "menu:about")))
async def cb_service_info(cq: CallbackQuery, db_user: User | None) -> None:
    """Экран «Информация»: о боте, ссылки на документы, кнопка поддержки снизу."""
    if await reject_if_no_user(cq, db_user) or await reject_if_blocked(cq, db_user):
        return
    assert db_user is not None
    settings = get_settings()
    sup = support_telegram_url(settings.support_username)
    privacy = (settings.info_privacy_policy_url or "").strip()
    terms = (settings.info_terms_of_service_url or "").strip()

    help_line = plain("Нужна помощь — нажмите «Поддержка» ниже.")

    doc_privacy = (
        plain("🔒 ") + link("Политика конфиденциальности", privacy)
        if privacy
        else plain("🔒 Политика конфиденциальности — задайте INFO_PRIVACY_POLICY_URL в .env.")
    )
    doc_terms = (
        plain("📋 ") + link("Пользовательское соглашение", terms)
        if terms
        else plain("📋 Пользовательское соглашение — задайте INFO_TERMS_OF_SERVICE_URL в .env.")
    )

    cap = join_lines(
        "💡 " + bold("Информация"),
        "",
        plain(
            "Этот сервис помогает увереннее пользоваться интернетом: за счёт устойчивых "
            "маршрутов и работы через московские сервера там, где это уместно. Параллельно "
            "режется навязчивая реклама и лишние трекеры, трафик проходит с более разумной "
            "фильтрацией — меньше шума и лишних запросов в фоне."
        ),
        "",
        plain(
            "Оформить и продлить доступ, пополнить баланс и управлять устройствами можно "
            "прямо в этом боте. В базовую подписку уже входят 2 устройства; при необходимости "
            "можно докупить слоты и пользоваться до 10 устройствами — например, телефон, "
            "планшет и домашний ПК."
        ),
        "",
        "📄 " + bold("Документы:"),
        doc_privacy,
        doc_terms,
        "",
        help_line,
    )
    await answer_callback_with_photo_screen(
        cq,
        caption=cap,
        reply_markup=_service_info_keyboard(sup).as_markup(),
        settings=settings,
    )
