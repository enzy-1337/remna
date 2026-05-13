"""ID-бот: показывает Telegram ID пользователя и чата.

Запуск: python -m idbot.main

Поведение:
- /start в личке -> "Имя / Тэг / Юзер ID" (ID копируется тапом).
- /start в личке c deep-link payload `cid_<chatid>` -> добавляет «Чат ID» из ссылки.
- /start и /id в группе/супергруппе/канале (без аргументов и без реплая) -> меню «куда отправить»;
  затем ID этого чата можно получить в группу и/или в ЛС. Если бот не может писать в ЛС —
  даёт кнопку с deep-link на @<bot>?start=cid_<chatid>.
- При добавлении бота в чат -> ничего не отправляет.
- При старте процесса -> BOOT-уведомление в админ-лог (ADMIN_LOG_CHAT_ID / ADMIN_LOG_TOPIC_BOOT).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
)

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from idbot.config import IdBotSettings, get_idbot_settings  # noqa: E402
from idbot.group_id_prompt import (  # noqa: E402
    REPLY_DEST_CALLBACK_PREFIX,
    display_name,
    parse_reply_destination_cb,
    send_group_id_destination_prompt,
)
from idbot.id_card import cta_keyboard, format_user_telegram_card  # noqa: E402
from idbot.user_id_lookup import router as id_lookup_router  # noqa: E402
from shared.md2 import bold, join_lines, plain  # noqa: E402

logger = logging.getLogger("idbot")

router = Router()

_CID_PAYLOAD_PREFIX = "cid_"


def _open_dm_keyboard(bot_username: str, chat_id: int) -> InlineKeyboardMarkup | None:
    uname = (bot_username or "").strip().lstrip("@")
    if not uname:
        return None
    payload = f"{_CID_PAYLOAD_PREFIX}{chat_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📩 Открыть в личке",
                    url=f"https://t.me/{uname}?start={payload}",
                    style="primary",
                )
            ],
        ]
    )


def _parse_chat_payload(args: str | None) -> int | None:
    raw = (args or "").strip()
    if not raw.startswith(_CID_PAYLOAD_PREFIX):
        return None
    tail = raw[len(_CID_PAYLOAD_PREFIX) :]
    try:
        return int(tail)
    except ValueError:
        return None


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start_private(message: Message, command: CommandObject) -> None:
    settings = get_idbot_settings()
    tg = message.from_user
    if tg is None:
        return
    name = display_name(tg.first_name, tg.last_name)
    kb = cta_keyboard(settings.bot_username)

    chat_id_from_payload = _parse_chat_payload(command.args)
    if chat_id_from_payload is not None:
        text = format_user_telegram_card(
            name=name,
            username=tg.username,
            user_id=tg.id,
            chat_id=chat_id_from_payload,
        )
    else:
        text = format_user_telegram_card(name=name, username=tg.username, user_id=tg.id)
    await message.answer(text, reply_markup=kb)


@router.message(CommandStart())
async def cmd_start_group(message: Message) -> None:
    """/start в группе/супергруппе/канале: предложить выбор «куда ответить»."""
    if message.chat.type == ChatType.PRIVATE:
        return  # подстраховка: приватный кейс ловит cmd_start_private

    await send_group_id_destination_prompt(message)


async def _send_fallback_with_deeplink(
    *, bot: Bot, chat_id: int, thread_id: int | None, settings: IdBotSettings, clicker_name: str, clicker_username: str | None
) -> None:
    """Если ЛС закрыты — отдаём в группу подсказку с deep-link на бота."""
    bot_uname = (settings.bot_username or "").strip().lstrip("@")
    if not bot_uname:
        try:
            me = await bot.get_me()
            bot_uname = (me.username or "").strip()
        except Exception:
            bot_uname = ""
    if not bot_uname:
        return
    kb_dm = _open_dm_keyboard(bot_uname, chat_id)
    mention_md = bold(f"@{clicker_username}") if clicker_username else bold(clicker_name)
    notice = mention_md + plain(", ЛС закрыты — откройте бота в личке и нажмите ") + bold("Start") + plain(".")
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=notice,
            reply_markup=kb_dm,
            message_thread_id=thread_id,
            disable_notification=True,
        )
    except Exception:
        logger.debug("idbot: не удалось отправить fallback-уведомление в чат %s", chat_id)


@router.callback_query(F.data.startswith(REPLY_DEST_CALLBACK_PREFIX))
async def cb_choose_destination(cq: CallbackQuery) -> None:
    """Обработка кнопок «В группу / В личку / И туда, и туда»."""
    parsed = parse_reply_destination_cb(cq.data or "")
    if parsed is None:
        await cq.answer()
        return
    kind, target_uid = parsed

    clicker = cq.from_user
    msg = cq.message
    if clicker is None or msg is None:
        await cq.answer()
        return

    if clicker.id != target_uid:
        await cq.answer("Это меню не для вас 🙂", show_alert=False)
        return

    settings = get_idbot_settings()
    bot = cq.bot
    name = display_name(clicker.first_name, clicker.last_name)
    cta_kb = cta_keyboard(settings.bot_username)

    # Текст для группы и для ЛС: тот же блок с Чат ID — для пользователя это самое полезное.
    id_text = format_user_telegram_card(
        name=name,
        username=clicker.username,
        user_id=clicker.id,
        chat_id=msg.chat.id,
    )

    dm_ok = False
    if kind in ("D", "B"):
        try:
            await bot.send_message(chat_id=clicker.id, text=id_text, reply_markup=cta_kb)
            dm_ok = True
        except TelegramForbiddenError:
            logger.info("idbot: ЛС закрыты у tg=%s", clicker.id)
        except TelegramBadRequest as e:
            logger.info("idbot: ЛС не доставлено tg=%s: %s", clicker.id, e)
        except Exception:
            logger.exception("idbot: ошибка отправки ЛС tg=%s", clicker.id)

    if kind in ("G", "B"):
        # Заменяем сообщение с меню на финальный текст в группе.
        try:
            await msg.edit_text(id_text, reply_markup=cta_kb)
        except Exception:
            # Фолбэк: попытаться удалить и отправить новое в ту же тему.
            try:
                await msg.delete()
            except Exception:
                pass
            try:
                await bot.send_message(
                    chat_id=msg.chat.id,
                    text=id_text,
                    reply_markup=cta_kb,
                    message_thread_id=msg.message_thread_id,
                )
            except Exception:
                logger.exception("idbot: не удалось отправить ID в группу %s", msg.chat.id)
    else:
        # kind == "D": в группе ничего не оставляем — убираем сообщение с меню.
        try:
            await msg.delete()
        except Exception:
            pass

    # Дополнения по итогам
    if kind == "D" and not dm_ok:
        await _send_fallback_with_deeplink(
            bot=bot,
            chat_id=msg.chat.id,
            thread_id=msg.message_thread_id,
            settings=settings,
            clicker_name=name,
            clicker_username=clicker.username,
        )
        await cq.answer("ЛС закрыты — отправил подсказку в группу.", show_alert=False)
        return

    if kind == "B" and not dm_ok:
        await cq.answer("ЛС закрыты — отправил только в группу.", show_alert=False)
        return

    if kind == "D" and dm_ok:
        await cq.answer("Отправил в личку ✓", show_alert=False)
        return

    await cq.answer("Готово ✓", show_alert=False)


async def _on_startup(bot: Bot, settings: IdBotSettings) -> None:
    try:
        await bot.set_my_commands(
            commands=[
                BotCommand(command="start", description="Показать ваш Telegram ID"),
                BotCommand(command="id", description="Узнать ID по @username или ответу"),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await bot.set_my_commands(
            commands=[
                BotCommand(
                    command="id",
                    description="Ответьте на сообщение или /id @username",
                ),
            ],
            scope=BotCommandScopeAllGroupChats(),
        )
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        logger.exception("idbot: set_my_commands / menu button failed")

    chat_id = settings.admin_log_chat_id
    if chat_id is None or (isinstance(chat_id, str) and not chat_id.strip()):
        return
    boot_ts = datetime.now(UTC).astimezone(ZoneInfo("Europe/Moscow")).strftime("%H:%M:%S | %d-%m-%Y | МСК")
    boot_text = join_lines(
        "🆔 " + bold("ID bot запущен"),
        plain(boot_ts),
    )
    try:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=settings.admin_log_topic_boot or settings.admin_log_topic_id,
            text=boot_text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception:
        logger.exception("idbot: BOOT-уведомление не доставлено")


async def _run() -> None:
    settings = get_idbot_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = (settings.idbot_bot_token or "").strip()
    if not token:
        raise RuntimeError("IDBOT_BOT_TOKEN is empty")

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.include_router(id_lookup_router)

    await _on_startup(bot, settings)

    allowed_updates = dp.resolve_used_update_types()
    await dp.start_polling(bot, allowed_updates=allowed_updates)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
