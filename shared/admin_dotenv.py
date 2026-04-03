"""Патч .env из web-admin: только белый список ключей (без токенов и DATABASE_URL)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from shared.config import Settings, get_settings


def _env_opt_int(v: int | None) -> str:
    return "" if v is None else str(v)


# (section_id, заголовок вкладки, [(ENV_KEY, короткий заголовок, getter, длинная подсказка как в .env.example)])
WEB_ADMIN_ENV_SECTIONS: list[tuple[str, str, list[tuple[str, str, Callable[[Settings], object], str]]]] = [
    (
        "panel",
        "Админка и сайт",
        [
            (
                "ADMIN_PANEL_TITLE",
                "Название в шапке",
                lambda s: s.admin_panel_title,
                "Подпись рядом с логотипом в боковом меню web-admin.",
            ),
            (
                "ADMIN_PANEL_LOGO_URL",
                "URL логотипа",
                lambda s: s.admin_panel_logo_url or "",
                "Картинка в шапке админки и иконка вкладки браузера (favicon). Укажите полный https://…",
            ),
            (
                "WEB_ADMIN_PROFILE_DISPLAY_NAME",
                "Имя в профиле админки",
                lambda s: s.web_admin_profile_display_name or "",
                "Подпись на странице «Мой профиль» (если пусто — имя из Telegram/GitHub при входе).",
            ),
            (
                "PUBLIC_SITE_URL",
                "Публичный URL",
                lambda s: s.public_site_url or "",
                "HTTPS-origin без слэша в конце — тот же домен, что в nginx для API. Нужен для Telegram Login Widget и абсолютных ссылок; в BotFather /setdomain — тот же хост.",
            ),
            (
                "BOT_USERNAME",
                "Username бота",
                lambda s: s.bot_username or "",
                "Без @. Для виджета входа в web-admin и реферальных ссылок.",
            ),
            (
                "SUPPORT_USERNAME",
                "Поддержка (username)",
                lambda s: s.support_username or "",
                "Username поддержки без @ (кнопки «написать» и т.п.).",
            ),
            (
                "REQUIRED_CHANNEL_USERNAME",
                "Канал (обязательная подписка)",
                lambda s: s.required_channel_username,
                "Username канала без @ для кнопки «Подписаться».",
            ),
        ],
    ),
    (
        "remnawave",
        "Remnawave",
        [
            (
                "REMNAWAVE_API_URL",
                "URL API панели",
                lambda s: s.remnawave_api_url,
                "Только origin панели, без пути к API (например https://panel.example.com).",
            ),
            (
                "REMNAWAVE_API_PATH_PREFIX",
                "Префикс API",
                lambda s: s.remnawave_api_path_prefix,
                "Часто /api; при 404 попробуйте пусто или /panel/api.",
            ),
            (
                "REMNAWAVE_PUBLIC_URL",
                "Публичный URL панели",
                lambda s: s.remnawave_public_url or "",
                "Домен, который пользователи открывают в VPN-клиенте, если REMNAWAVE_API_URL внутренний (docker/localhost).",
            ),
            (
                "REMNAWAVE_REQUEST_TIMEOUT",
                "Таймаут запросов (сек)",
                lambda s: str(s.remnawave_request_timeout),
                "Таймаут HTTP к API Remnawave.",
            ),
            (
                "REMNAWAVE_SYNC_ENABLED",
                "Синхронизация включена",
                lambda s: "true" if s.remnawave_sync_enabled else "false",
                "Фоновая синхронизация Remnawave → БД.",
            ),
            (
                "REMNAWAVE_SYNC_INTERVAL_SEC",
                "Интервал синхронизации (сек)",
                lambda s: str(s.remnawave_sync_interval_sec),
                "Как часто подтягивать пользователей из панели.",
            ),
            (
                "REMNAWAVE_SYNC_IMPORT_LIMIT",
                "Лимит импорта за проход",
                lambda s: str(s.remnawave_sync_import_limit),
                "Максимум записей пользователей Remnawave за один проход.",
            ),
            (
                "REMNAWAVE_SYNC_PUSH_DESCRIPTION",
                "Пушить description в панель",
                lambda s: "true" if s.remnawave_sync_push_description else "false",
                "Обновлять description в Remnawave при синхронизации (имя, tg и т.д.).",
            ),
            (
                "REMNAWAVE_STUB",
                "Режим заглушки",
                lambda s: "true" if s.remnawave_stub else "false",
                "true — не ходить в API (локальные тесты).",
            ),
        ],
    ),
    (
        "subs",
        "Подписки и устройства",
        [
            (
                "TRIAL_DURATION_DAYS",
                "Длительность триала (дней)",
                lambda s: str(s.trial_duration_days),
                "Сколько дней даётся пробный период.",
            ),
            (
                "TRIAL_TRAFFIC_GB",
                "Трафик триала (ГБ)",
                lambda s: str(s.trial_traffic_gb),
                "Лимит трафика на триал.",
            ),
            (
                "EXTRA_DEVICE_PRICE_RUB",
                "Цена доп. устройства (₽)",
                lambda s: str(s.extra_device_price_rub),
                "Стоимость одного дополнительного слота устройства.",
            ),
            (
                "SUBSCRIPTION_AUTORENEW_ENABLED",
                "Автопродление подписки",
                lambda s: "true" if s.subscription_autorenew_enabled else "false",
                "Списание с баланса и продление за ~1 ч до конца подписки.",
            ),
            (
                "SUBSCRIPTION_AUTORENEW_INTERVAL_SEC",
                "Интервал проверки автопродления (сек)",
                lambda s: str(s.subscription_autorenew_interval_sec),
                "Как часто проверять подписки на автопродление.",
            ),
            (
                "SUBSCRIPTION_AUTORENEW_WINDOW_SEC",
                "Окно автопродления (сек)",
                lambda s: str(s.subscription_autorenew_window_sec),
                "За сколько секунд до expires_at пытаться продлить (по умолчанию 1 ч).",
            ),
            (
                "SUBSCRIPTION_EXPIRY_NOTIFY_ENABLED",
                "Напоминания об окончании",
                lambda s: "true" if s.subscription_expiry_notify_enabled else "false",
                "Уведомления в Telegram за ~24 ч и ~3 ч до конца подписки/триала.",
            ),
            (
                "SUBSCRIPTION_EXPIRY_NOTIFY_INTERVAL_SEC",
                "Интервал проверки напоминаний (сек)",
                lambda s: str(s.subscription_expiry_notify_interval_sec),
                "Как часто проверять подписки на напоминания.",
            ),
        ],
    ),
    (
        "ref",
        "Рефералы и отчёты",
        [
            (
                "REFERRAL_SIGNUP_BONUS_RUB",
                "Бонус за регистрацию по ссылке (₽)",
                lambda s: str(s.referral_signup_bonus_rub),
                "Начисление приглашённому и пригласившему при входе по реф-ссылке (0 = выкл.).",
            ),
            (
                "REFERRAL_INVITER_REWARD_RUB_PER_30_DAYS",
                "Награда пригласившему ₽ / 30 дн.",
                lambda s: str(s.referral_inviter_reward_rub_per_30_days),
                "За первую платную покупку приглашённого: ₽ на каждые 30 дн. купленного тарифа.",
            ),
            (
                "REFERRAL_INVITER_REWARD_DAYS_PER_30_DAYS",
                "Награда пригласившему дней / 30 дн.",
                lambda s: str(s.referral_inviter_reward_days_per_30_days),
                "Дней к активной подписке пригласившего на каждые 30 дн. периода.",
            ),
            (
                "ADMIN_REPORT_ENABLED",
                "Ежедневный отчёт в админ-чат",
                lambda s: "true" if s.admin_report_enabled else "false",
                "Plain-текст отчёт в тему REPORTS.",
            ),
            (
                "ADMIN_REPORT_HOUR_UTC",
                "Час отчёта (UTC)",
                lambda s: str(s.admin_report_hour_utc),
                "Час UTC для ежедневного отчёта.",
            ),
            (
                "ADMIN_REPORT_TIMEZONE",
                "Часовой пояс отчёта",
                lambda s: s.admin_report_timezone,
                "Таймзона для границ «вчера» в отчёте (напр. Europe/Moscow).",
            ),
        ],
    ),
    (
        "admin_log",
        "Админ-чат и темы",
        [
            (
                "ADMIN_LOG_CHAT_ID",
                "ID чата (форум)",
                lambda s: str(s.admin_log_chat_id) if s.admin_log_chat_id is not None else "",
                "Супергруппа с включёнными темами: id вида -100… Куда уходят уведомления бота.",
            ),
            (
                "ADMIN_LOG_TOPIC_ID",
                "Тема по умолчанию",
                lambda s: _env_opt_int(s.admin_log_topic_id),
                "Если для типа события не задана своя тема — используется эта (число id темы в Telegram).",
            ),
            (
                "ADMIN_LOG_TOPIC_BOOT",
                "Тема: запуск бота",
                lambda s: _env_opt_int(s.admin_log_topic_boot),
                "Сообщение «бот запущен» при старте процесса. Пусто — как общая тема (GENERAL или TOPIC_ID).",
            ),
            (
                "ADMIN_LOG_TOPIC_GENERAL",
                "Тема: общее",
                lambda s: _env_opt_int(s.admin_log_topic_general),
                "",
            ),
            (
                "ADMIN_LOG_TOPIC_PAYMENTS",
                "Тема: платежи",
                lambda s: _env_opt_int(s.admin_log_topic_payments),
                "",
            ),
            (
                "ADMIN_LOG_TOPIC_USERS",
                "Тема: пользователи",
                lambda s: _env_opt_int(s.admin_log_topic_users),
                "",
            ),
            (
                "ADMIN_LOG_TOPIC_TRIALS",
                "Тема: триалы",
                lambda s: _env_opt_int(s.admin_log_topic_trials),
                "",
            ),
            (
                "ADMIN_LOG_TOPIC_BONUSES",
                "Тема: бонусы",
                lambda s: _env_opt_int(s.admin_log_topic_bonuses),
                "",
            ),
            (
                "ADMIN_LOG_TOPIC_SUBSCRIPTIONS",
                "Тема: подписки",
                lambda s: _env_opt_int(s.admin_log_topic_subscriptions),
                "",
            ),
            (
                "ADMIN_LOG_TOPIC_PROMO",
                "Тема: промокоды",
                lambda s: _env_opt_int(s.admin_log_topic_promo),
                "",
            ),
            (
                "ADMIN_LOG_TOPIC_DEVICES",
                "Тема: устройства",
                lambda s: _env_opt_int(s.admin_log_topic_devices),
                "",
            ),
            (
                "ADMIN_LOG_TOPIC_SUPPORT",
                "Тема: поддержка",
                lambda s: _env_opt_int(s.admin_log_topic_support),
                "",
            ),
            (
                "ADMIN_LOG_TOPIC_BACKUPS",
                "Тема: бэкапы",
                lambda s: _env_opt_int(s.admin_log_topic_backups),
                "",
            ),
            (
                "ADMIN_LOG_TOPIC_REPORTS",
                "Тема: отчёты",
                lambda s: _env_opt_int(s.admin_log_topic_reports),
                "",
            ),
        ],
    ),
    (
        "tech",
        "Техническое",
        [
            (
                "MAINTENANCE_MODE",
                "Режим обслуживания",
                lambda s: "true" if s.maintenance_mode else "false",
                "Ограничение работы бота для пользователей.",
            ),
            (
                "LOG_LEVEL",
                "Уровень логов",
                lambda s: s.log_level,
                "Например INFO, DEBUG, WARNING.",
            ),
            (
                "DEBUG",
                "DEBUG",
                lambda s: "true" if s.debug else "false",
                "Режим отладки приложения.",
            ),
            (
                "BACKUP_ENABLED",
                "Бэкап БД в Telegram",
                lambda s: "true" if s.backup_enabled else "false",
                "Ежедневный pg_dump в тему BACKUPS (нужен pg_dump и ADMIN_LOG_CHAT_ID).",
            ),
            (
                "BACKUP_HOUR_UTC",
                "Час бэкапа (UTC)",
                lambda s: str(s.backup_hour_utc),
                "Час UTC для ежедневного бэкапа PostgreSQL.",
            ),
            (
                "BACKUP_TIMEZONE",
                "Таймзона бэкапа",
                lambda s: s.backup_timezone,
                "Например Europe/Moscow. Используется с BACKUP_HOUR_LOCAL.",
            ),
            (
                "BACKUP_HOUR_LOCAL",
                "Час бэкапа (локальный)",
                lambda s: "" if s.backup_hour_local is None else str(s.backup_hour_local),
                "Если задан, имеет приоритет над BACKUP_HOUR_UTC.",
            ),
            (
                "BACKUP_MAX_TELEGRAM_MB",
                "Макс. размер файла бэкапа (МБ)",
                lambda s: str(s.backup_max_telegram_mb),
                "Лимит размера для отправки в Telegram (~50 МБ у ботов).",
            ),
            (
                "CHANNEL_SUB_CACHE_TTL",
                "TTL кэша подписки на канал (сек)",
                lambda s: str(s.channel_sub_cache_ttl),
                "Кэш Redis проверки подписки на обязательный канал.",
            ),
            (
                "INSTRUCTION_ANDROID_URL",
                "Инструкция Android (URL)",
                lambda s: s.instruction_android_url or "",
                "Ссылка с экрана инструкций.",
            ),
            (
                "INSTRUCTION_IOS_URL",
                "Инструкция iOS (URL)",
                lambda s: s.instruction_ios_url or "",
                "Ссылка с экрана инструкций.",
            ),
            (
                "PLATEGA_API_BASE_URL",
                "Platega API base URL",
                lambda s: s.platega_api_base_url,
                "Базовый URL API Platega (из документации).",
            ),
            (
                "PLATEGA_PAYMENT_METHOD",
                "Platega метод оплаты (число)",
                lambda s: str(s.platega_payment_method),
                "Идентификатор способа оплаты в Platega.",
            ),
        ],
    ),
]

WEB_ADMIN_ENV_WHITELIST: list[tuple[str, str, Callable[[Settings], object]]] = [
    (key, label, getter) for _, _, items in WEB_ADMIN_ENV_SECTIONS for key, label, getter, _ in items
]

ALLOWED_ENV_KEYS = frozenset(key for key, _, _ in WEB_ADMIN_ENV_WHITELIST)


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
