from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

router = Router(name="tickets_admin")


@router.callback_query(F.data.startswith("tickets:status:"))
async def cb_status_stub(cq: CallbackQuery) -> None:
    # Полная логика будет на шаге 7.
    await cq.answer("Ок.", show_alert=False)


@router.callback_query(F.data.startswith("tickets:close:"))
async def cb_close_stub(cq: CallbackQuery) -> None:
    # Полная логика будет на шаге 7.
    await cq.answer("Ок.", show_alert=False)


@router.callback_query(F.data.startswith("tickets:reply:"))
async def cb_reply_stub(cq: CallbackQuery) -> None:
    # В норме тут будет deep link (шаг 6). Если URL-кнопка не сгенерилась — просто отвечаем.
    await cq.answer("Откройте тикет-бота для ответа.", show_alert=True)

