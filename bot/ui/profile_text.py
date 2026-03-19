"""Тексты главного экрана «Профиль» (MarkdownV2)."""

from __future__ import annotations

from aiogram.types import User as TgUser

from shared.md2 import bold, code, italic, join_lines, plain, quote_block, spoiler, strike, underline
from shared.models.user import User


def profile_caption(db_user: User, tg_user: TgUser) -> str:
    # Нельзя делать esc() до bold/code — обёртки сами экранируют содержимое.
    display_name = tg_user.first_name or db_user.first_name or "—"
    # Примеры форматирования MarkdownV2 (как в ТЗ)
    sep = plain(" · ")
    fmt_demo = (
        bold("жирный")
        + sep
        + italic("курсив")
        + sep
        + underline("подчёркнутый")
        + sep
        + strike("зачёркнутый")
        + sep
        + spoiler("скрытый")
        + sep
        + code("mono")
    )
    return join_lines(
        "👤 " + bold("Профиль:"),
        "",
        plain("📝 Имя: ") + bold(display_name),
        plain("🆔 ID: ") + code(str(tg_user.id)),
        plain("💳 Баланс: ") + bold(f"{db_user.balance:.2f}") + plain(" ₽"),
        "",
        plain("✨ ") + fmt_demo,
        "",
        quote_block("Совет: сохраните ссылку подписки в надёжном месте."),
    )
