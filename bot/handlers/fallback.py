"""Подсказка /start для подписанных на канал, но ещё не зарегистрированных."""

from aiogram import Router
from aiogram.types import Message

from bot.filters.registration import UnregisteredChannelMemberTextFilter
from shared.md2 import esc

router = Router(name="fallback")


@router.message(UnregisteredChannelMemberTextFilter())
async def prompt_start(message: Message) -> None:
    await message.answer(esc("Нажмите /start, чтобы зарегистрироваться и открыть меню."))
