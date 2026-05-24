"""Тексты главного экрана «Профиль» (MarkdownV2)."""

from __future__ import annotations

from aiogram.types import User as TgUser

from shared.md2 import bold, code, join_lines, plain
from shared.models.user import User


def profile_caption(db_user: User, tg_user: TgUser, *, is_admin: bool = False) -> str:
    # Нельзя делать esc() до bold/code — обёртки сами экранируют содержимое.
    display_name = tg_user.first_name or db_user.first_name or "—"

    # Блок с данными пользователя делаем цитатой (MarkdownV2): строки начинаются с `>`.
    lines = [
        "> " + (plain("📝 Имя: ") + bold(display_name)),
        "> " + (plain("🆔 ID: ") + code(str(tg_user.id))),
    ]
    if is_admin:
        github_label = db_user.github_username or "не привязан"
        lines.append("> " + (plain("🐙 GitHub: ") + bold(github_label)))
    lines.append(
        "> "
        + (
            plain("💳 Баланс: ")
            + bold(f"{db_user.balance:.2f}")
            + plain(" ₽")
        )
    )
    profile_quote = "\n".join(lines)
    return join_lines(
        "👤 " + bold("Профиль:"),
        "",
        profile_quote,
    )
