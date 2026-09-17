"""2FA (TOTP) личного кабинета сайта: секрет/QR/бэкап-коды поверх pyotp (тот же стек, что у web-admin)."""

from __future__ import annotations

import hashlib
import secrets
import string

import pyotp
import segno

BACKUP_CODES_COUNT = 10
_BACKUP_ALPHABET = string.ascii_uppercase + string.digits


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_qr_data_uri(*, secret: str, account_name: str) -> str:
    uri = pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name="Flux Network")
    return segno.make(uri).png_data_uri(scale=5)


def verify_totp_code(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code or not secret:
        return False
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=1)
    except Exception:
        return False


def _hash_backup_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _normalize_backup_code(raw: str) -> str:
    return (raw or "").strip().upper().replace("-", "").replace(" ", "")


def generate_backup_codes() -> tuple[list[str], list[dict]]:
    """Возвращает (коды для показа пользователю один раз, записи для хранения в БД)."""
    plain_codes: list[str] = []
    stored: list[dict] = []
    for _ in range(BACKUP_CODES_COUNT):
        raw = "".join(secrets.choice(_BACKUP_ALPHABET) for _ in range(8))
        display = f"{raw[:4]}-{raw[4:]}"
        plain_codes.append(display)
        stored.append({"hash": _hash_backup_code(raw), "used": False})
    return plain_codes, stored


def verify_and_consume_backup_code(backup_codes: list[dict] | None, raw_code: str) -> tuple[bool, list[dict]]:
    """Проверяет резервный код и (если верен) помечает его использованным.

    Возвращает (успех, обновлённый список записей для сохранения в БД)."""
    normalized = _normalize_backup_code(raw_code)
    if not normalized or not backup_codes:
        return False, backup_codes or []
    target_hash = _hash_backup_code(normalized)
    updated = list(backup_codes)
    for entry in updated:
        if not isinstance(entry, dict) or entry.get("used"):
            continue
        if entry.get("hash") == target_hash:
            entry["used"] = True
            return True, updated
    return False, updated


def count_unused_backup_codes(backup_codes: list[dict] | None) -> int:
    if not backup_codes:
        return 0
    return sum(1 for e in backup_codes if isinstance(e, dict) and not e.get("used"))
