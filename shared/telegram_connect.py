"""Ожидание доступности Telegram Bot API и безопасная настройка меню при старте."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, MenuButtonCommands

PROXY_HINT = (
    "Проверьте доступ к api.telegram.org. На VPS обычно нужен SOCKS5: "
    "BOT_PROXYCHAINS_ENABLED=true, PROXYCHAINS_SOCKS5_HOST=host.docker.internal, "
    "PROXYCHAINS_SOCKS5_PORT=1080 (см. README «Бот через VPN»)."
)

log = logging.getLogger(__name__)


async def wait_telegram_online(
    bot: Bot,
    *,
    service: str,
    attempts: int = 360,
    delay_sec: float = 10.0,
) -> None:
    """
    Повторяет getMe, пока Telegram API недоступен (блокировка / прокси ещё не поднят).
    Успешный вызов кэширует bot.me — start_polling не дергает сеть повторно.
    """
    last_err: BaseException | None = None
    for n in range(1, attempts + 1):
        try:
            await bot.get_me()
            if n > 1:
                log.info("%s: связь с Telegram API восстановлена (попытка %s)", service, n)
            return
        except (TelegramNetworkError, OSError, ConnectionError) as e:
            last_err = e
            if n == 1 or n % 6 == 0:
                log.warning(
                    "%s: Telegram API недоступен (%s). Попытка %s/%s, пауза %.0f с. %s",
                    service,
                    e,
                    n,
                    attempts,
                    delay_sec,
                    PROXY_HINT,
                )
            await asyncio.sleep(delay_sec)
    msg = f"{service}: Telegram API недоступен после {attempts} попыток (~{int(attempts * delay_sec)} с)"
    raise RuntimeError(msg) from last_err


async def safe_set_bot_commands(
    bot: Bot,
    *,
    service: str,
    private_commands: list[BotCommand],
    group_commands: list[BotCommand] | None = None,
) -> None:
    """setMyCommands / menu button — не валят процесс при сетевой ошибке."""
    try:
        await bot.set_my_commands(
            commands=private_commands,
            scope=BotCommandScopeAllPrivateChats(),
        )
        if group_commands:
            await bot.set_my_commands(
                commands=group_commands,
                scope=BotCommandScopeAllGroupChats(),
            )
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        log.exception("%s: set_my_commands / menu button failed", service)
