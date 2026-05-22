"""Строки для админ-уведомления о применении промокода (MarkdownV2)."""

from __future__ import annotations

from shared.md2 import bold, code, plain


def promo_apply_admin_notify_lines(meta: dict) -> list[str]:
    """Подписи полей уведомления по типу промокода (без ₽ для extra_days и т.п.)."""
    code_s = str(meta.get("code") or "")
    type_s = str(meta.get("type") or "")
    value_s = str(meta.get("value") or "")
    mt = type_s.strip().lower()

    lines: list[str] = [
        plain("Код: ") + code(code_s),
        plain("Тип: ") + code(type_s),
    ]
    if mt == "topup_bonus_percent":
        lines.append(plain("Бонус: +") + bold(value_s) + plain("%"))
        lines.append(
            plain("Сработает 1 раз на первое пополнение после активации."),
        )
    elif mt == "extra_days":
        lines.append(plain("Дни: +") + bold(value_s))
    elif mt == "extra_gb":
        lines.append(plain("ГБ: +") + bold(value_s))
    elif mt == "extra_devices":
        lines.append(plain("Устройства: +") + bold(value_s))
    elif mt == "discount_percent":
        lines.append(plain("Скидка: ") + bold(value_s) + plain("%"))
    elif mt in {"balance_rub", "bonus_rub"}:
        lines.append(plain("Сумма: ") + bold(value_s) + plain(" ₽"))
    else:
        lines.append(plain("Значение: ") + bold(value_s))
    return lines
