"""Тексты главного экрана «Профиль» (MarkdownV2)."""

from __future__ import annotations

from aiogram.types import User as TgUser

from shared.md2 import bold, code, join_lines, plain, quote_block
from shared.models.user import User


def profile_caption(db_user: User, tg_user: TgUser) -> str:
    # Нельзя делать esc() до bold/code — обёртки сами экранируют содержимое.
    display_name = tg_user.first_name or db_user.first_name or "—"

    # Блок с данными пользователя делаем цитатой (MarkdownV2): строки начинаются с `>`.
    profile_quote = "\n".join(
        [
            "> " + (plain("📝 Имя: ") + bold(display_name)),
            "> " + (plain("🆔 ID: ") + code(str(tg_user.id))),
            "> "
            + (
                plain("💳 Баланс: ")
                + bold(f"{db_user.balance:.2f}")
                + plain(" ₽")
            ),
        ]
    )
    return join_lines(
        "👤 " + bold("Профиль:"),
        "",
        profile_quote,
        # "",
        # quote_block("Совет: сохраните ссылку подписки в надёжном месте."),
    )
