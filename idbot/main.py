"""ID-бот: показывает Telegram ID пользователя и чата.

Запуск: python -m idbot.main

Поведение:
- /start в личке -> "Имя / Тэг / Юзер ID" (ID копируется тапом).
- /start в личке c deep-link payload `cid_<chatid>` -> добавляет «Чат ID» из ссылки.
- /start в группе/супергруппе/канале -> сообщение пользователя удаляется,
  ID этого чата отправляется в личные сообщения. Если бот не может писать в ЛС —
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
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from idbot.config import IdBotSettings, get_idbot_settings  # noqa: E402
from shared.md2 import bold, code, esc, italic, join_lines, plain  # noqa: E402

logger = logging.getLogger("idbot")

router = Router()

_CID_PAYLOAD_PREFIX = "cid_"
# Префикс callback_data для меню выбора места ответа в группе.
# Формат: "idbot:r:<G|D|B>:<user_id>".  G=group, D=dm, B=both.
_CB_REPLY_PREFIX = "idbot:r:"


def _cta_keyboard(bot_username: str | None) -> InlineKeyboardMarkup | None:
    """Inline-кнопка-«приписка» в стиле reels-бота: 💜 @<bot_username>."""
    uname = (bot_username or "").strip().lstrip("@")
    if not uname:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💜 @{uname}",
                    url=f"https://t.me/{uname}",
                    style="primary",
                )
            ],
        ]
    )


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


def _destination_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Меню выбора: куда отправить ID-ответ в группе."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 В группу",
                    callback_data=f"{_CB_REPLY_PREFIX}G:{user_id}",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📩 В личку",
                    callback_data=f"{_CB_REPLY_PREFIX}D:{user_id}",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌐 И туда, и туда",
                    callback_data=f"{_CB_REPLY_PREFIX}B:{user_id}",
                    style="success",
                ),
            ],
        ]
    )


def _parse_reply_cb(data: str) -> tuple[str, int] | None:
    """Разбор callback_data меню выбора: ('G'|'D'|'B', target_user_id)."""
    if not data or not data.startswith(_CB_REPLY_PREFIX):
        return None
    rest = data[len(_CB_REPLY_PREFIX) :]
    parts = rest.split(":", 1)
    if len(parts) != 2:
        return None
    kind, uid_s = parts
    if kind not in ("G", "D", "B"):
        return None
    try:
        return kind, int(uid_s)
    except ValueError:
        return None


def _display_name(first_name: str | None, last_name: str | None) -> str:
    parts = [(first_name or "").strip(), (last_name or "").strip()]
    full = " ".join(p for p in parts if p)
    return full or "—"


def _header_line(*, has_chat: bool) -> str:
    """Жирный заголовок сообщения; меняется в зависимости от контекста."""
    title = "Информация о чате" if has_chat else "Ваш Telegram"
    return "🪪 " + bold(title)


def _format_user_lines(*, name: str, username: str | None, user_id: int) -> list[str]:
    tag = f"@{username}" if username else "—"
    # Пробел НЕ внутри bold — иначе у Telegram изредка не распознаётся сущность.
    return [
        plain("👤 ") + bold("Имя") + plain(": ") + esc(name),
        plain("🏷 ") + bold("Тэг") + plain(": ") + esc(tag),
        plain("🆔 ") + bold("Юзер ID") + plain(": ") + code(str(user_id)),
    ]


def _format_chat_line(chat_id: int) -> str:
    return plain("💬 ") + bold("Чат ID") + plain(": ") + code(str(chat_id))


def _hint_copy_line() -> str:
    # Маленькая подсказка курсивом — намёк, что моноширинный ID копируется тапом.
    return italic("Тапните по ID, чтобы скопировать.")


def _private_text(*, name: str, username: str | None, user_id: int) -> str:
    return join_lines(
        _header_line(has_chat=False),
        "",
        *_format_user_lines(name=name, username=username, user_id=user_id),
        "",
        _hint_copy_line(),
    )


def _group_text(*, name: str, username: str | None, user_id: int, chat_id: int) -> str:
    return join_lines(
        _header_line(has_chat=True),
        "",
        *_format_user_lines(name=name, username=username, user_id=user_id),
        _format_chat_line(chat_id),
        "",
        _hint_copy_line(),
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
    name = _display_name(tg.first_name, tg.last_name)
    kb = _cta_keyboard(settings.bot_username)

    chat_id_from_payload = _parse_chat_payload(command.args)
    if chat_id_from_payload is not None:
        text = _group_text(
            name=name,
            username=tg.username,
            user_id=tg.id,
            chat_id=chat_id_from_payload,
        )
    else:
        text = _private_text(name=name, username=tg.username, user_id=tg.id)
    await message.answer(text, reply_markup=kb)


@router.message(CommandStart())
async def cmd_start_group(message: Message) -> None:
    """/start в группе/супергруппе/канале: предложить выбор «куда ответить»."""
    if message.chat.type == ChatType.PRIVATE:
        return  # подстраховка: приватный кейс ловит cmd_start_private

    tg = message.from_user
    if tg is None or tg.is_bot:
        return

    bot = message.bot
    name = _display_name(tg.first_name, tg.last_name)
    mention_md = bold(f"@{tg.username}") if tg.username else bold(name)
    prompt = join_lines(
        mention_md + plain(", куда отправить ваш ID?"),
        italic("Выбор доступен только вам."),
    )
    kb = _destination_keyboard(tg.id)

    try:
        await message.delete()
    except Exception:
        logger.debug("idbot: не удалось удалить /start в чате %s", message.chat.id)

    try:
        await bot.send_message(
            chat_id=message.chat.id,
            text=prompt,
            reply_markup=kb,
            message_thread_id=message.message_thread_id,
            disable_notification=True,
        )
    except Exception:
        logger.exception("idbot: не удалось отправить меню выбора в чат %s", message.chat.id)


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


@router.callback_query(F.data.startswith(_CB_REPLY_PREFIX))
async def cb_choose_destination(cq: CallbackQuery) -> None:
    """Обработка кнопок «В группу / В личку / И туда, и туда»."""
    parsed = _parse_reply_cb(cq.data or "")
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
    name = _display_name(clicker.first_name, clicker.last_name)
    cta_kb = _cta_keyboard(settings.bot_username)

    # Текст для группы и для ЛС: тот же блок с Чат ID — для пользователя это самое полезное.
    id_text = _group_text(
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
            commands=[BotCommand(command="start", description="Показать ваш Telegram ID")],
            scope=BotCommandScopeAllPrivateChats(),
        )
    except Exception:
        logger.exception("idbot: set_my_commands failed")

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

    await _on_startup(bot, settings)

    allowed_updates = dp.resolve_used_update_types()
    await dp.start_polling(bot, allowed_updates=allowed_updates)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
