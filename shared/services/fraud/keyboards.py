"""Инлайн-клавиатуры алертов антифрода — сырой JSON (Bot API), см. telegram_notify.send_telegram_message."""

from __future__ import annotations


def suspicion_keyboard(incident_id: int) -> dict:
    """Подозрение (learning/advisory или autonomous с недостаточной уверенностью): решение за админом."""
    return {
        "inline_keyboard": [
            [
                {"text": "🚫 Заблокировать", "callback_data": f"fraud:block:{incident_id}"},
                {"text": "👁 На учёт", "callback_data": f"fraud:watch:{incident_id}"},
                {"text": "✅ Пропустить", "callback_data": f"fraud:dismiss:{incident_id}"},
            ]
        ]
    }


def autoblock_keyboard(incident_id: int, profile_url: str | None) -> dict:
    """Автоблокировка (жёсткое правило / blacklist / autonomous с высокой уверенностью)."""
    row = [{"text": "✅ Разблокировать", "callback_data": f"fraud:unblock:{incident_id}"}]
    if profile_url:
        row.append({"text": "🔎 В админке", "url": profile_url})
    return {"inline_keyboard": [row]}
