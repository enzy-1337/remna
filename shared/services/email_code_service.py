"""Код подтверждения почты (6 цифр) — вход на сайт по email и привязка почты в боте.
Отправка: обычный SMTP (Gmail/Yandex/Mail.ru — бесплатно, без подтверждения домена) в приоритете,
либо Resend REST API (https://resend.com), если SMTP не настроен.
"""

from __future__ import annotations

import logging
import secrets

import redis.asyncio as redis

from shared.config import Settings, get_settings
from shared.services.email_sender import email_sending_configured, send_branded_email

__all__ = ["email_sending_configured", "normalize_email", "start_email_code", "verify_email_code"]

logger = logging.getLogger(__name__)

_CODE_KEY = "email_code:{purpose}:{email}"
_ATTEMPTS_KEY = "email_code_attempts:{purpose}:{email}"
_COOLDOWN_KEY = "email_code_cooldown:{purpose}:{email}"
_CODE_TTL_SEC = 600  # 10 минут на ввод кода
_COOLDOWN_SEC = 60  # не чаще раза в минуту на один email
_MAX_ATTEMPTS = 5


def _client(url: str) -> redis.Redis:
    return redis.from_url(url, encoding="utf-8", decode_responses=True)


def normalize_email(raw: str) -> str | None:
    email = (raw or "").strip().lower()
    if "@" not in email or " " in email or len(email) > 255:
        return None
    local, _, domain = email.partition("@")
    if not local or "." not in domain or len(domain) < 3:
        return None
    return email


async def start_email_code(
    email: str, *, purpose: str, settings: Settings | None = None
) -> tuple[bool, str]:
    """Генерирует и отправляет код. Возвращает (ok, error_message_if_any)."""
    s = settings or get_settings()
    if not email_sending_configured(s):
        return False, "Отправка писем ещё не настроена."
    try:
        r = _client(s.redis_url)
        try:
            cooldown_key = _COOLDOWN_KEY.format(purpose=purpose, email=email)
            if await r.get(cooldown_key):
                return False, "Код уже отправлен — подождите минуту перед повторной отправкой."
            code = f"{secrets.randbelow(1_000_000):06d}"
            code_key = _CODE_KEY.format(purpose=purpose, email=email)
            attempts_key = _ATTEMPTS_KEY.format(purpose=purpose, email=email)
            pipe = r.pipeline()
            pipe.set(code_key, code, ex=_CODE_TTL_SEC)
            pipe.delete(attempts_key)
            pipe.set(cooldown_key, "1", ex=_COOLDOWN_SEC)
            await pipe.execute()
        finally:
            await r.aclose()
    except Exception:
        logger.exception("start_email_code redis failed")
        return False, "Внутренняя ошибка, попробуйте позже."

    sent = await _send_code_email(email, code, settings=s)
    if not sent:
        return False, "Не удалось отправить письмо, попробуйте позже."
    return True, ""


async def verify_email_code(
    email: str, code: str, *, purpose: str, settings: Settings | None = None
) -> tuple[bool, str]:
    """Возвращает (ok, error_message_if_any). Успешная проверка одноразовая."""
    s = settings or get_settings()
    code = (code or "").strip()
    if not code:
        return False, "Введите код."
    try:
        r = _client(s.redis_url)
        try:
            code_key = _CODE_KEY.format(purpose=purpose, email=email)
            attempts_key = _ATTEMPTS_KEY.format(purpose=purpose, email=email)
            attempts = int(await r.incr(attempts_key))
            if attempts == 1:
                await r.expire(attempts_key, _CODE_TTL_SEC)
            if attempts > _MAX_ATTEMPTS:
                await r.delete(code_key)
                return False, "Слишком много попыток — запросите новый код."
            expected = await r.get(code_key)
            if not expected:
                return False, "Код истёк или не был запрошен — запросите новый."
            if not secrets.compare_digest(str(expected), code):
                return False, "Неверный код."
            pipe = r.pipeline()
            pipe.delete(code_key)
            pipe.delete(attempts_key)
            await pipe.execute()
        finally:
            await r.aclose()
    except Exception:
        logger.exception("verify_email_code redis failed")
        return False, "Внутренняя ошибка, попробуйте позже."
    return True, ""


async def _send_code_email(email: str, code: str, *, settings: Settings) -> bool:
    ok, _err = await send_branded_email(
        email,
        subject=f"Код подтверждения: {code}",
        title="Код подтверждения",
        intro="Введите этот код, чтобы подтвердить почту в Flux Network.",
        big_code=code,
        note="Код действует 10 минут. Если вы его не запрашивали — просто проигнорируйте письмо.",
        badge="Безопасность",
        settings=settings,
    )
    return ok
