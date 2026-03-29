"""Патч .env из web-admin: только белый список ключей (без токенов и DATABASE_URL)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from shared.config import Settings, get_settings

WEB_ADMIN_ENV_WHITELIST: list[tuple[str, str, Callable[[Settings], str]]] = [
    ("ADMIN_PANEL_TITLE", "Название в шапке админки", lambda s: s.admin_panel_title),
    ("ADMIN_PANEL_LOGO_URL", "URL логотипа (https://…)", lambda s: s.admin_panel_logo_url or ""),
    ("PUBLIC_SITE_URL", "Публичный URL сайта (HTTPS)", lambda s: s.public_site_url or ""),
    ("BOT_USERNAME", "BOT_USERNAME (без @)", lambda s: s.bot_username or ""),
    ("SUPPORT_USERNAME", "SUPPORT_USERNAME (без @)", lambda s: s.support_username or ""),
    ("REQUIRED_CHANNEL_USERNAME", "Канал: username без @", lambda s: s.required_channel_username),
    ("REMNAWAVE_PUBLIC_URL", "REMNAWAVE_PUBLIC_URL", lambda s: s.remnawave_public_url or ""),
    ("REMNAWAVE_API_URL", "REMNAWAVE_API_URL (origin)", lambda s: s.remnawave_api_url),
    ("TRIAL_DURATION_DAYS", "Дней триала", lambda s: str(s.trial_duration_days)),
    ("EXTRA_DEVICE_PRICE_RUB", "Цена доп. устройства (₽)", lambda s: str(s.extra_device_price_rub)),
    ("REFERRAL_SIGNUP_BONUS_RUB", "Реф. бонус при регистрации (₽)", lambda s: str(s.referral_signup_bonus_rub)),
    ("LOG_LEVEL", "LOG_LEVEL", lambda s: s.log_level),
    ("MAINTENANCE_MODE", "MAINTENANCE_MODE (true/false)", lambda s: "true" if s.maintenance_mode else "false"),
    ("BACKUP_ENABLED", "BACKUP_ENABLED (true/false)", lambda s: "true" if s.backup_enabled else "false"),
    ("ADMIN_REPORT_ENABLED", "ADMIN_REPORT_ENABLED (true/false)", lambda s: "true" if s.admin_report_enabled else "false"),
]

ALLOWED_ENV_KEYS = frozenset(k for k, _, _ in WEB_ADMIN_ENV_WHITELIST)


def default_dotenv_path() -> Path:
    return Path(".env").resolve()


def _fmt_value_for_dotenv(raw: str) -> str:
    val = raw.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in val:
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if val == "":
        return ""
    if any(c in val for c in ' #"\'') or val.startswith(" "):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return val


def read_whitelist_values() -> dict[str, str]:
    s = get_settings()
    out: dict[str, str] = {}
    for key, _label, fn in WEB_ADMIN_ENV_WHITELIST:
        v = fn(s)
        if v is None:
            out[key] = ""
        elif isinstance(v, bool):
            out[key] = "true" if v else "false"
        else:
            out[key] = str(v).strip()
    return out


def patch_dotenv(updates: dict[str, str], *, path: Path | None = None) -> None:
    """Обновляет или добавляет строки KEY=value; остальной файл не трогает."""
    p = path or default_dotenv_path()
    filtered = {k: v for k, v in updates.items() if k in ALLOWED_ENV_KEYS}
    if not filtered:
        return
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    lines = text.splitlines(keepends=True)
    if not lines and text.endswith("\n"):
        lines = [text]
    elif not lines and text:
        lines = [text if text.endswith("\n") else text + "\n"]
    done: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        s_line = line.lstrip()
        if not s_line.strip() or s_line.lstrip().startswith("#"):
            new_lines.append(line)
            continue
        if "=" not in line:
            new_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in filtered:
            v = _fmt_value_for_dotenv(filtered[key])
            new_lines.append(f"{key}={v}\n")
            done.add(key)
        else:
            new_lines.append(line)
    for key, raw in filtered.items():
        if key not in done:
            v = _fmt_value_for_dotenv(raw)
            new_lines.append(f"{key}={v}\n")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(new_lines), encoding="utf-8")
    get_settings.cache_clear()
