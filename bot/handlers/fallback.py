"""Подсказка /start для подписанных на канал, но ещё не зарегистрированных."""

from aiogram import Router
from aiogram.types import Message

from bot.filters.registration import UnregisteredChannelMemberTextFilter

router = Router(name="fallback")


@router.message(UnregisteredChannelMemberTextFilter())
async def prompt_start(message: Message) -> None:
    await message.answer("Нажмите /start, чтобы зарегистрироваться и открыть меню.")
