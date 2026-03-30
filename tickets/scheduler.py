from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from sqlalchemy import text

from shared.database import get_session_factory
from tickets.config import config

log = logging.getLogger("tickets.scheduler")


def _admin_mentions_html() -> str:
    if not config.admin_ids:
        return ""
    parts = [f'<a href="tg://user?id={aid}">admin:{aid}</a>' for aid in config.admin_ids]
    return " ".join(parts)


class TicketScheduler:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._reminder_loop(), name="tickets-reminder-loop"),
            asyncio.create_task(self._autoclose_loop(), name="tickets-autoclose-loop"),
        ]
        log.info("Ticket scheduler started.")

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("Ticket scheduler stopped.")

    async def _reminder_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._run_reminder_once()
            except Exception:
                log.exception("Reminder loop iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1800)
            except asyncio.TimeoutError:
                pass

    async def _autoclose_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._run_autoclose_once()
            except Exception:
                log.exception("Autoclose loop iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=21600)
            except asyncio.TimeoutError:
                pass

    async def _run_reminder_once(self) -> None:
        if config.support_group_id == 0:
            return
        factory = get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT t.id, t.topic_id,
                               EXTRACT(EPOCH FROM (NOW() - t.created_at)) / 3600.0 AS no_reply_hours
                        FROM tickets t
                        WHERE t.status = 'open'
                          AND t.created_at <= NOW() - (:hrs * INTERVAL '1 hour')
                          AND NOT EXISTS (
                            SELECT 1
                            FROM ticket_messages m
                            WHERE m.ticket_id = t.id
                              AND m.sender_role = 'admin'
                              AND COALESCE(m.is_internal, false) = false
                          )
                        ORDER BY t.id DESC
                        """
                    ),
                    {"hrs": int(config.reminder_hours)},
                )
            ).all()
        mentions = _admin_mentions_html()
        for tid, topic_id, no_reply_hours in rows:
            try:
                hours_i = max(1, int(float(no_reply_hours or 0)))
            except Exception:
                hours_i = int(config.reminder_hours)
            msg = (
                f"⏰ Тикет #{int(tid)} не получил ответа уже {hours_i} часов!"
                + (f"\n\n{mentions}" if mentions else "")
            )
            try:
                await self.bot.send_message(
                    chat_id=config.support_group_id,
                    message_thread_id=int(topic_id),
                    text=msg,
                    disable_web_page_preview=True,
                )
            except Exception:
                log.exception("Failed to send reminder for ticket #%s", tid)

    async def _run_autoclose_once(self) -> None:
        if config.support_group_id == 0:
            return
        factory = get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT id, topic_id, telegram_user_id
                        FROM tickets
                        WHERE status IN ('open','in_progress')
                          AND COALESCE(last_activity, created_at) <= NOW() - (:days * INTERVAL '1 day')
                        ORDER BY id ASC
                        """
                    ),
                    {"days": int(config.auto_close_days)},
                )
            ).all()
            for tid, topic_id, tg_uid in rows:
                now = datetime.now(timezone.utc)
                await session.execute(
                    text(
                        """
                        UPDATE tickets
                        SET status = 'closed',
                            updated_at = :now,
                            last_activity = :now,
                            closed_at = :now
                        WHERE id = :tid
                        """
                    ),
                    {"tid": int(tid), "now": now},
                )
            await session.commit()

        for tid, topic_id, tg_uid in rows:
            try:
                await self.bot.close_forum_topic(
                    chat_id=config.support_group_id,
                    message_thread_id=int(topic_id),
                )
            except Exception:
                log.exception("Failed to close forum topic for ticket #%s", tid)
            try:
                uid = int(tg_uid or 0)
                if uid:
                    await self.bot.send_message(
                        chat_id=uid,
                        text=f"Ваш тикет #{int(tid)} автоматически закрыт в связи с неактивностью",
                    )
            except Exception:
                log.exception("Failed to notify user about autoclose ticket #%s", tid)

