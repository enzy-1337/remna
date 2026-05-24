"""Тексты главного экрана «Профиль» (MarkdownV2)."""

from __future__ import annotations

from decimal import Decimal

from aiogram.types import User as TgUser

from shared.md2 import bold, code, italic, join_lines, plain
from shared.models.user import User
from shared.services.subscription_service import (
    user_custom_month_price_rub,
    user_personal_discount_percent,
)


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
    custom_month = user_custom_month_price_rub(db_user)
    personal_disc = user_personal_discount_percent(db_user)
    if custom_month is not None:
        lines.append(
            "> "
            + (
                plain("💰 Ваш тариф: ")
                + bold(str(custom_month.quantize(Decimal("0.01"))))
                + plain(" ₽/мес")
            )
        )
    elif personal_disc > 0:
        lines.append(
            "> "
            + (plain("🏷 Ваша скидка: ") + bold(str(personal_disc)) + plain("%"))
        )
    profile_quote = "\n".join(lines)
    return join_lines(
        "👤 " + bold("Профиль:"),
        "",
        profile_quote,
        # "",
        # quote_block("Совет: сохраните ссылку подписки в надёжном месте."),
    )
