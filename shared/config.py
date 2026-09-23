"""Общие настройки приложения (Pydantic Settings)."""

from decimal import Decimal
from functools import lru_cache

from pydantic import AliasChoices, Field, computed_field, field_validator, model_validator

from shared.bot_cta import DEFAULT_BOT_CTA_LABEL
from shared.services.admin_log_topics import AdminLogTopic
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Пустые ADMIN_TELEGRAM_IDS= / ADMIN_TELEGRAM_ID= не ломают разбор
        env_ignore_empty=True,
    )

    # Telegram
    bot_token: str = Field(..., validation_alias="BOT_TOKEN")
    required_channel_id: int | str = Field(..., validation_alias="REQUIRED_CHANNEL_ID")
    required_channel_username: str = Field(
        ...,
        validation_alias="REQUIRED_CHANNEL_USERNAME",
        description="Username канала без @ для кнопки «Подписаться»",
    )
    bot_username: str | None = Field(
        default=None,
        validation_alias="BOT_USERNAME",
        description="Username бота без @ (для реф. ссылок на шаге 9)",
    )
    bot_cta_label: str = Field(
        default=DEFAULT_BOT_CTA_LABEL,
        validation_alias="BOT_CTA_LABEL",
        description="Подпись рекламной inline-кнопки в ботах. Подстановка: {username} — username без @",
    )
    bot_profile_short_description: str | None = Field(
        default=None,
        validation_alias="BOT_PROFILE_SHORT_DESCRIPTION",
        description="Краткое описание бота в Telegram (поиск, до ~120 символов). Пусто — встроенный текст.",
    )
    bot_profile_description: str | None = Field(
        default=None,
        validation_alias="BOT_PROFILE_DESCRIPTION",
        description="Текст «О боте» в профиле Telegram до нажатия Start (до ~512 символов). Пусто — встроенный текст.",
    )
    downloader_bot_token: str = Field(
        default="",
        validation_alias="DOWNLOADER_BOT_TOKEN",
        description="Токен отдельного downloader-бота (Instagram Reels / Shorts / TikTok).",
    )
    downloader_forum_chat_id: int | None = Field(
        default=None,
        validation_alias="DOWNLOADER_FORUM_CHAT_ID",
        description="ID forum-группы, где создаются персональные топики пользователей downloader-бота.",
    )
    downloader_max_file_mb: int = Field(
        default=0,
        ge=0,
        validation_alias="DOWNLOADER_MAX_FILE_MB",
        description="Ограничение размера отправляемого видео в МБ (0 = без лимита).",
    )
    downloader_max_duration_sec: int = Field(
        default=0,
        ge=0,
        validation_alias="DOWNLOADER_MAX_DURATION_SEC",
        description="Ограничение длительности в секундах (0 = без лимита).",
    )
    support_username: str | None = Field(
        default=None,
        validation_alias="SUPPORT_USERNAME",
        description="Поддержка: username без @",
    )
    legal_privacy_source_url: str = Field(
        default="https://telegra.ph/Politika-konfidencialnosti-08-01-83",
        validation_alias="LEGAL_PRIVACY_SOURCE_URL",
        description="Откуда один раз импортировать политику конфиденциальности на сайт (/legal/privacy).",
    )
    legal_terms_source_url: str = Field(
        default="https://telegra.ph/Polzovatelskoe-soglashenie-08-01-39",
        validation_alias="LEGAL_TERMS_SOURCE_URL",
        description="Откуда один раз импортировать пользовательское соглашение на сайт (/legal/terms).",
    )
    info_privacy_policy_url: str = Field(
        default="https://telegra.ph/Politika-konfidencialnosti-08-15-17",
        validation_alias="INFO_PRIVACY_POLICY_URL",
        description="Ссылка на политику конфиденциальности (экран «Информация»)",
    )
    info_terms_of_service_url: str = Field(
        default="https://telegra.ph/Polzovatelskoe-soglashenie-08-15-10",
        validation_alias="INFO_TERMS_OF_SERVICE_URL",
        description="Ссылка на пользовательское соглашение (экран «Информация»)",
    )
    instruction_android_url: str | None = Field(
        default=None,
        validation_alias="INSTRUCTION_ANDROID_URL",
    )
    instruction_ios_url: str | None = Field(
        default=None,
        validation_alias="INSTRUCTION_IOS_URL",
    )
    instruction_macos_url: str | None = Field(
        default=None,
        validation_alias="INSTRUCTION_MACOS_URL",
    )
    instruction_telegraph_phone_url: str | None = Field(
        default=None,
        validation_alias="INSTRUCTION_TELEGRAPH_PHONE_URL",
        description="Telegra.ph — инструкция для телефона",
    )
    instruction_telegraph_pc_url: str | None = Field(
        default=None,
        validation_alias="INSTRUCTION_TELEGRAPH_PC_URL",
        description="Telegra.ph — инструкция для ПК",
    )

    # Картинка для экранов (профиль, подписка, …): файл или URL
    bot_section_photo_path: str | None = Field(
        default=None,
        validation_alias="BOT_SECTION_PHOTO_PATH",
    )
    bot_section_photo_url: str | None = Field(
        default=None,
        validation_alias="BOT_SECTION_PHOTO_URL",
    )
    telegram_webhook_enabled: bool = Field(
        default=False,
        validation_alias="TELEGRAM_WEBHOOK_ENABLED",
        description="Входящие апдейты бота через POST /webhooks/telegram (uvicorn); polling в bot.main отключается",
    )
    telegram_webhook_url: str = Field(
        default="",
        validation_alias="TELEGRAM_WEBHOOK_URL",
        description="Полный HTTPS URL для Bot API setWebhook (должен совпадать с маршрутом API, например …/webhooks/telegram)",
    )
    telegram_webhook_secret: str = Field(
        default="",
        validation_alias="TELEGRAM_WEBHOOK_SECRET",
        description="Секрет для заголовка X-Telegram-Bot-Api-Secret-Token (рекомендуется ≥16 символов)",
    )

    # Админ-лог (шаг 12): чат или супергруппа; topic_id — ID темы в форуме
    admin_log_chat_id: str | int | None = Field(
        default=None,
        validation_alias="ADMIN_LOG_CHAT_ID",
    )
    admin_log_topic_id: int | None = Field(
        default=None,
        validation_alias="ADMIN_LOG_TOPIC_ID",
        description="Тема по умолчанию, если не задана отдельная для типа события",
    )
    admin_log_topic_general: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_GENERAL")
    admin_log_topic_payments: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_PAYMENTS")
    admin_log_topic_users: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_USERS")
    admin_log_topic_trials: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_TRIALS")
    admin_log_topic_bonuses: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_BONUSES")
    admin_log_topic_subscriptions: int | None = Field(
        default=None, validation_alias="ADMIN_LOG_TOPIC_SUBSCRIPTIONS"
    )
    admin_log_topic_promo: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_PROMO")
    admin_log_topic_devices: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_DEVICES")
    admin_log_topic_support: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_SUPPORT")
    admin_log_topic_login: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_LOGIN")
    admin_log_topic_backups: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_BACKUPS")
    admin_log_topic_reports: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_REPORTS")
    admin_log_topic_boot: int | None = Field(
        default=None,
        validation_alias="ADMIN_LOG_TOPIC_BOOT",
        description="Тема форума для сообщений о запуске бота",
    )
    admin_log_topic_channel: int | None = Field(
        default=None,
        validation_alias="ADMIN_LOG_TOPIC_CHANNEL",
        description="Тема форума для событий подписки/отписки канала",
    )
    admin_log_topic_errors: int | None = Field(
        default=None,
        validation_alias="ADMIN_LOG_TOPIC_ERRORS",
        description="Тема форума для технических ошибок (необработанные исключения)",
    )
    admin_log_topic_fraud_suspicion: int | None = Field(
        default=None,
        validation_alias="ADMIN_LOG_TOPIC_FRAUD_SUSPICION",
        description="Тема форума для подозрений антифрода (кнопки Заблокировать/На учёт/Пропустить)",
    )
    admin_log_topic_fraud_autoblock: int | None = Field(
        default=None,
        validation_alias="ADMIN_LOG_TOPIC_FRAUD_AUTOBLOCK",
        description="Тема форума для автоблокировок антифрода (кнопки Разблокировать/В админку)",
    )
    admin_report_enabled: bool = Field(default=False, validation_alias="ADMIN_REPORT_ENABLED")
    admin_report_hour_utc: int = Field(
        default=8,
        ge=0,
        le=23,
        validation_alias="ADMIN_REPORT_HOUR_UTC",
        description="Час UTC для ежедневного отчёта в админ-чат",
    )
    admin_report_timezone: str = Field(
        default="Europe/Moscow",
        validation_alias="ADMIN_REPORT_TIMEZONE",
        description="Часовой пояс для границ «вчера» в отчёте (напр. Europe/Moscow)",
    )
    admin_telegram_id: int | None = Field(
        default=None,
        validation_alias="ADMIN_TELEGRAM_ID",
        description="Устарело: используйте SUPERADMIN_TELEGRAM_ID. Оставлено для совместимости.",
    )
    superadmin_telegram_id: int | None = Field(
        default=None,
        validation_alias="SUPERADMIN_TELEGRAM_ID",
        description="Единственный супер-админ (Telegram user id). Может назначать админов в веб-панели.",
    )
    # В .env строка «1,2,3» или пусто — не list[int] (иначе pydantic-settings ждёт JSON и падает на "")
    admin_telegram_ids_csv: str = Field(
        default="",
        validation_alias="ADMIN_TELEGRAM_IDS",
        description="Устарело: список id для кнопки админ-панели; права задаются в admin_users.",
    )

    # Redis (кэш проверки подписки на канал)
    redis_url: str = Field(default="redis://localhost:6379/0", validation_alias="REDIS_URL")
    channel_sub_cache_ttl: int = Field(default=300, validation_alias="CHANNEL_SUB_CACHE_TTL")

    # PostgreSQL
    database_url: str = Field(..., validation_alias="DATABASE_URL")

    # Remnawave HTTPS API (отдельный VPS)
    remnawave_api_url: str = Field(
        default="https://remnawave.example.com",
        validation_alias="REMNAWAVE_API_URL",
        description="Только origin панели, без пути к API (например https://panel.example.com)",
    )
    remnawave_api_path_prefix: str = Field(
        default="/api",
        validation_alias="REMNAWAVE_API_PATH_PREFIX",
        description="Префикс API на nginx (часто /api; при 404 попробуйте пусто или /panel/api)",
    )
    remnawave_public_url: str | None = Field(
        default=None,
        validation_alias="REMNAWAVE_PUBLIC_URL",
        description=(
            "Публичный origin для ссылок подписки (https://panel.example.com). "
            "Если бот и панель на одном сервере и REMNAWAVE_API_URL внутренний "
            "(localhost, docker), укажите домен, который открывают пользователи в клиенте."
        ),
    )
    remnawave_api_token: str = Field(default="", validation_alias="REMNAWAVE_API_TOKEN")
    remnawave_default_squad_uuid: str | None = Field(
        default=None,
        validation_alias="REMNAWAVE_DEFAULT_SQUAD_UUID",
        description="Устарело для activeInternalSquads: всем выдаётся sub-opt (см. optimized_route_service).",
    )
    remnawave_optimized_squad_uuid: str | None = Field(
        default=None,
        validation_alias="REMNAWAVE_OPTIMIZED_SQUAD_UUID",
        description=(
            "Необязательно: переопределить UUID internal squad sub-opt для панели; "
            "если пусто — используется встроенный SUB_OPT_INTERNAL_SQUAD_UUID."
        ),
    )
    remnawave_cookie: str | None = Field(
        default=None,
        validation_alias="REMNAWAVE_COOKIE",
        description="Cookie для nginx reverse-proxy: либо значение (WbYWpixX), либо целиком NAME=VALUE (как в docs).",
    )
    remnawave_request_timeout: float = Field(default=10.0, validation_alias="REMNAWAVE_REQUEST_TIMEOUT")
    remnawave_sync_enabled: bool = Field(default=True, validation_alias="REMNAWAVE_SYNC_ENABLED")
    remnawave_sync_run_immediately: bool = Field(
        default=False,
        validation_alias="REMNAWAVE_SYNC_RUN_IMMEDIATELY",
        description="Если false — первый цикл sync стартует только после интервала, без массовой проверки сразу после boot.",
    )
    remnawave_sync_interval_sec: int = Field(
        default=1800,
        validation_alias="REMNAWAVE_SYNC_INTERVAL_SEC",
        description="Интервал фоновой синхронизации Remnawave -> БД (сек)",
    )
    remnawave_sync_import_limit: int = Field(
        default=300,
        validation_alias="REMNAWAVE_SYNC_IMPORT_LIMIT",
        description="Максимум записей пользователей Remnawave за один проход синхронизации",
    )
    remnawave_sync_push_description: bool = Field(
        default=True,
        validation_alias="REMNAWAVE_SYNC_PUSH_DESCRIPTION",
        description="Обновлять description в Remnawave при синхронизации (имя, tg, телефон и т.д.)",
    )
    remnawave_stub: bool = Field(
        default=False,
        validation_alias="REMNAWAVE_STUB",
        description="Не ходить в API; для локальных тестов",
    )
    remnawave_webhooks_enabled: bool = Field(default=False, validation_alias="REMNAWAVE_WEBHOOKS_ENABLED")
    remnawave_webhook_secret: str = Field(default="", validation_alias="REMNAWAVE_WEBHOOK_SECRET")
    remnawave_webhook_signature_ttl_sec: int = Field(
        default=300,
        ge=30,
        validation_alias="REMNAWAVE_WEBHOOK_SIGNATURE_TTL_SEC",
    )
    remnawave_webhook_background_process: bool = Field(
        default=True,
        validation_alias="REMNAWAVE_WEBHOOK_BACKGROUND_PROCESS",
    )
    bot_tariff_purchases_enabled: bool = Field(
        default=True,
        validation_alias="BOT_TARIFF_PURCHASES_ENABLED",
        description="Если false — в боте скрыты кнопки покупки тарифов и списание с баланса за тариф недоступно.",
    )
    billing_v2_enabled: bool = Field(default=False, validation_alias="BILLING_V2_ENABLED")
    billing_v2_for_new_users_only: bool = Field(
        default=False,
        validation_alias="BILLING_V2_FOR_NEW_USERS_ONLY",
        description=(
            "Устарело: при BILLING_V2_ENABLED новые пользователи hybrid, legacy переводится на hybrid "
            "после окончания подписки (и на /start). Значение больше не блокирует перевод."
        ),
    )
    billing_calendar_timezone: str = Field(
        default="Europe/Moscow",
        validation_alias="BILLING_CALENDAR_TIMEZONE",
        description=(
            "IANA-таймзона календарных суток: детализация, суточное списание за устройства, "
            "граница месяца для пакетного лимита ГБ (traffic_gb_step)"
        ),
    )
    billing_device_daily_job_interval_sec: int = Field(
        default=120,
        ge=60,
        validation_alias="BILLING_DEVICE_DAILY_JOB_INTERVAL_SEC",
        description="Интервал проверки «догонки» суточного списания за устройства (сек)",
    )
    billing_traffic_rw_meter_enabled: bool = Field(
        default=True,
        validation_alias="BILLING_TRAFFIC_RW_METER_ENABLED",
        description=(
            "Списание PAYG-ГБ по данным панели (ceil(used_gb) через опрос get_user); "
            "вебхуки traffic.gb_step помечаются ignored_meter_poll и не списывают повторно."
        ),
    )
    billing_traffic_meter_poll_interval_sec: int = Field(
        default=120,
        ge=30,
        validation_alias="BILLING_TRAFFIC_METER_POLL_INTERVAL_SEC",
        description="Интервал фонового опроса трафика в панели для счётчика ГБ (сек)",
    )

    # Антифрод (см. shared/services/fraud/): общий рубильник + по одному на детектор.
    # Детекторы ip_hop/hwid_collision/traffic_spike проходят staged rollout (FraudDetectorState:
    # learning -> advisory -> autonomous), жёсткое IP-правило и blacklist всегда немедленные.
    fraud_detection_enabled: bool = Field(default=False, validation_alias="FRAUD_DETECTION_ENABLED")
    fraud_hwid_collision_enabled: bool = Field(
        default=False, validation_alias="FRAUD_HWID_COLLISION_ENABLED"
    )
    fraud_ip_hop_enabled: bool = Field(default=False, validation_alias="FRAUD_IP_HOP_ENABLED")
    fraud_ip_hop_poll_interval_sec: int = Field(
        default=20,
        ge=15,
        validation_alias="FRAUD_IP_HOP_POLL_INTERVAL_SEC",
        description="Интервал опроса Remnawave connections API для детектора смены IP (сек)",
    )
    fraud_ip_hop_hard_block_ip_count: int = Field(
        default=10,
        ge=2,
        validation_alias="FRAUD_IP_HOP_HARD_BLOCK_IP_COUNT",
        description="Жёсткое правило: столько разных IP за окно ниже -> мгновенный автобан вне staged rollout",
    )
    fraud_ip_hop_hard_block_window_sec: int = Field(
        default=15,
        ge=1,
        validation_alias="FRAUD_IP_HOP_HARD_BLOCK_WINDOW_SEC",
    )
    fraud_ip_hop_suspicious_ip_count: int = Field(
        default=5,
        ge=2,
        validation_alias="FRAUD_IP_HOP_SUSPICIOUS_IP_COUNT",
        description="Мягкий порог (идёт через staged rollout, не мгновенный автобан)",
    )
    fraud_ip_hop_suspicious_window_sec: int = Field(
        default=30,
        ge=1,
        validation_alias="FRAUD_IP_HOP_SUSPICIOUS_WINDOW_SEC",
    )
    fraud_traffic_spike_enabled: bool = Field(
        default=False, validation_alias="FRAUD_TRAFFIC_SPIKE_ENABLED"
    )
    fraud_traffic_spike_gb_threshold: float = Field(
        default=1.25,
        gt=0,
        validation_alias="FRAUD_TRAFFIC_SPIKE_GB_THRESHOLD",
        description="Порог трафика (ГБ) за окно fraud_traffic_spike_window_sec для срабатывания",
    )
    fraud_traffic_spike_window_sec: int = Field(
        default=60,
        ge=10,
        validation_alias="FRAUD_TRAFFIC_SPIKE_WINDOW_SEC",
    )
    fraud_traffic_spike_poll_interval_sec: int = Field(
        default=30,
        ge=15,
        validation_alias="FRAUD_TRAFFIC_SPIKE_POLL_INTERVAL_SEC",
        description="Опрос трафика для антифрода — для ВСЕХ пользователей, не зависит от billing_mode",
    )
    fraud_blacklist_sync_enabled: bool = Field(
        default=False, validation_alias="FRAUD_BLACKLIST_SYNC_ENABLED"
    )
    fraud_blacklist_sync_interval_sec: int = Field(
        default=300,
        ge=60,
        validation_alias="FRAUD_BLACKLIST_SYNC_INTERVAL_SEC",
    )
    fraud_blacklist_url: str = Field(
        default="https://raw.githubusercontent.com/BEDOLAGA-DEV/VPN-BLACKLIST/refs/heads/main/blacklist.txt",
        validation_alias="FRAUD_BLACKLIST_URL",
    )
    billing_device_daily_rub: Decimal = Field(default=Decimal("2.5"), validation_alias="BILLING_DEVICE_DAILY_RUB")
    billing_gb_step_rub: Decimal = Field(default=Decimal("5"), validation_alias="BILLING_GB_STEP_RUB")
    billing_hybrid_hwid_slots: int = Field(
        default=15,
        ge=2,
        le=64,
        validation_alias="BILLING_HYBRID_HWID_SLOTS",
        description="Hybrid/PAYG: лимит HWID в панели и слотов подписки (без докупки; оплата за фактически использованные устройства по суточной ставке).",
    )
    broadcast_main_channel_id: int | None = Field(
        default=-1002701615639,
        validation_alias="BROADCAST_MAIN_CHANNEL_ID",
        description="ID канала для опциональной рассылки из web-admin (отрицательный id). 0 — выкл.",
    )
    billing_mobile_gb_extra_rub: Decimal = Field(default=Decimal("2.5"), validation_alias="BILLING_MOBILE_GB_EXTRA_RUB")
    billing_optimized_route_gb_extra_rub: Decimal = Field(
        default=Decimal("2.5"),
        validation_alias="BILLING_OPTIMIZED_ROUTE_GB_EXTRA_RUB",
        description="Доплата ₽ за 1 шаг pay-as-you-go ГБ при включённом «оптимизированном маршруте»",
    )
    billing_balance_floor_rub: Decimal = Field(default=Decimal("-50"), validation_alias="BILLING_BALANCE_FLOOR_RUB")
    billing_min_topup_rub: Decimal = Field(default=Decimal("10"), validation_alias="BILLING_MIN_TOPUP_RUB")
    billing_first_topup_fixed_bonus_rub: Decimal = Field(
        default=Decimal("10"),
        ge=Decimal("0"),
        le=Decimal("10000"),
        validation_alias="BILLING_FIRST_TOPUP_FIXED_BONUS_RUB",
        description=(
            "Фиксированный бонус в ₽ при первом успешном пополнении (если сумма не ниже порога "
            "BILLING_FIRST_TOPUP_EXTRA_BALANCE_MIN_RUB). 0 — выключено."
        ),
    )
    billing_first_topup_extra_balance_percent: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        le=Decimal("500"),
        validation_alias="BILLING_FIRST_TOPUP_EXTRA_BALANCE_PERCENT",
        description=(
            "Доп. начисление на баланс при **первом** успешном пополнении: процент от суммы **этого** платежа "
            "(без учёта промо-бонуса). 0 = выключено. 100 = удвоение основной суммы при пороге ниже."
        ),
    )
    billing_first_topup_extra_balance_min_rub: Decimal = Field(
        default=Decimal("10"),
        ge=Decimal("0"),
        validation_alias="BILLING_FIRST_TOPUP_EXTRA_BALANCE_MIN_RUB",
        description="Минимальная сумма пополнения (₽ из транзакции), с которой срабатывает BILLING_FIRST_TOPUP_EXTRA_BALANCE_PERCENT",
    )
    billing_first_topup_welcome_enabled: bool = Field(
        default=True,
        validation_alias="BILLING_FIRST_TOPUP_WELCOME_ENABLED",
        description=(
            "Мастер-переключатель welcome при первом пополнении без активной подписки (как отдельный флаг у триала). "
            "Если false — блок welcome не выполняется даже при BILLING_FIRST_TOPUP_WELCOME_GB > 0."
        ),
    )
    billing_first_topup_welcome_gb: int = Field(
        default=5,
        ge=0,
        le=1024,
        validation_alias="BILLING_FIRST_TOPUP_WELCOME_GB",
        description=(
            "Сколько первых ГБ в PAYG не тарифицируются (списание за шаг ГБ не выполняется); лимит в панели не меняется. "
            "0 отключает. Идемпотентность по транзакции `welcome_gb_bonus:{user_id}` без изменений."
        ),
    )
    billing_legacy_lifetime_cutoff_year: int = Field(
        default=2099,
        ge=2030,
        validation_alias="BILLING_LEGACY_LIFETIME_CUTOFF_YEAR",
    )
    billing_transition_base_month_rub: Decimal = Field(
        default=Decimal("130"),
        validation_alias="BILLING_TRANSITION_BASE_MONTH_RUB",
    )
    billing_transition_fee_percent: Decimal = Field(
        default=Decimal("10"),
        validation_alias="BILLING_TRANSITION_FEE_PERCENT",
    )
    billing_transition_check_interval_sec: int = Field(
        default=1800,
        ge=5,
        validation_alias="BILLING_TRANSITION_CHECK_INTERVAL_SEC",
    )
    billing_detail_retention_days: int = Field(default=183, ge=30, validation_alias="BILLING_DETAIL_RETENTION_DAYS")
    billing_negative_notify_enabled: bool = Field(
        default=True,
        validation_alias="BILLING_NEGATIVE_NOTIFY_ENABLED",
    )
    billing_negative_notify_interval_sec: int = Field(
        default=900,
        ge=120,
        validation_alias="BILLING_NEGATIVE_NOTIFY_INTERVAL_SEC",
    )

    # Триал
    trial_enabled: bool = Field(
        default=False,
        validation_alias="TRIAL_ENABLED",
        description="Если false — кнопка триала скрыта для всех пользователей",
    )
    trial_duration_days: int = Field(default=3, validation_alias="TRIAL_DURATION_DAYS")
    trial_traffic_gb: int = Field(default=1, validation_alias="TRIAL_TRAFFIC_GB")

    # Подписка / устройства (шаг 8)
    extra_device_price_rub: Decimal = Field(
        default=Decimal("50"),
        validation_alias="EXTRA_DEVICE_PRICE_RUB",
        description="Стоимость одного дополнительного слота устройства при покупке с баланса (₽ за слот).",
    )
    subscription_included_device_slots: int = Field(
        default=2,
        ge=1,
        le=64,
        validation_alias="SUBSCRIPTION_INCLUDED_DEVICE_SLOTS",
        description="Сколько слотов устройств входит в базовую подписку без отдельной доплаты (закладка под месячный биллинг; бот пока не списывает).",
    )
    extra_device_monthly_rub: Decimal = Field(
        default=Decimal("50"),
        validation_alias="EXTRA_DEVICE_MONTHLY_RUB",
        description="Справочная цена ₽/мес за слот сверх включённых (отображение; списание — EXTRA_DEVICE_PRICE_RUB).",
    )
    subscription_autorenew_enabled: bool = Field(
        default=True,
        validation_alias="SUBSCRIPTION_AUTORENEW_ENABLED",
        description="Фоновое списание с баланса и +1 мес. за ~1 ч до конца подписки",
    )
    subscription_autorenew_interval_sec: int = Field(
        default=300,
        ge=60,
        validation_alias="SUBSCRIPTION_AUTORENEW_INTERVAL_SEC",
        description="Как часто проверять подписки на автопродление (сек)",
    )
    subscription_autorenew_window_sec: int = Field(
        default=3600,
        ge=300,
        le=86400,
        validation_alias="SUBSCRIPTION_AUTORENEW_WINDOW_SEC",
        description="За сколько секунд до expires_at пытаться продлить (по умолчанию 1 ч)",
    )
    subscription_renewal_window_days: int = Field(
        default=7,
        ge=0,
        le=90,
        validation_alias="SUBSCRIPTION_RENEWAL_WINDOW_DAYS",
        description="Продление тарифом с баланса: только если до конца подписки осталось не больше N дней (0 = без ограничения)",
    )
    subscription_repeat_purchase_bonus_percent: Decimal = Field(
        default=Decimal("5"),
        ge=0,
        le=100,
        validation_alias="SUBSCRIPTION_REPEAT_PURCHASE_BONUS_PERCENT",
        description="Бонус на основной баланс при 2-й и далее покупке тарифа с баланса, % от списанной суммы (0 = выкл.)",
    )
    subscription_expiry_notify_enabled: bool = Field(
        default=True,
        validation_alias="SUBSCRIPTION_EXPIRY_NOTIFY_ENABLED",
        description="Уведомления в Telegram за ~24 ч и ~3 ч до конца подписки/триала",
    )
    subscription_expiry_notify_interval_sec: int = Field(
        default=300,
        ge=120,
        validation_alias="SUBSCRIPTION_EXPIRY_NOTIFY_INTERVAL_SEC",
        description="Как часто проверять подписки на напоминания (сек)",
    )
    onboarding_app_name: str = Field(default="Happ", validation_alias="ONBOARDING_APP_NAME",
        description="Приложение, которое рекомендуем в подсказках «как подключиться».")
    onboarding_app_ios_url: str = Field(default="https://apps.apple.com/app/happ-proxy-utility/id6504287215",
        validation_alias="ONBOARDING_APP_IOS_URL")
    onboarding_app_android_url: str = Field(default="https://play.google.com/store/apps/details?id=com.happproxy",
        validation_alias="ONBOARDING_APP_ANDROID_URL")
    onboarding_app_windows_url: str = Field(default="https://www.happ.su/main", validation_alias="ONBOARDING_APP_WINDOWS_URL")
    onboarding_app_macos_url: str = Field(default="https://apps.apple.com/app/happ-proxy-utility/id6504287215",
        validation_alias="ONBOARDING_APP_MACOS_URL")
    intro_offer_enabled: bool = Field(
        default=True,
        validation_alias="INTRO_OFFER_ENABLED",
        description="Разовая акция для тех, кто ни разу не покупал подписку: INTRO_OFFER_DAYS дней за INTRO_OFFER_PRICE_RUB ₽.",
    )
    intro_offer_days: int = Field(default=14, ge=1, le=90, validation_alias="INTRO_OFFER_DAYS")
    intro_offer_price_rub: Decimal = Field(default=Decimal("1"), ge=0, validation_alias="INTRO_OFFER_PRICE_RUB")
    intro_offer_old_price_rub: Decimal = Field(
        default=Decimal("99"), ge=0, validation_alias="INTRO_OFFER_OLD_PRICE_RUB",
        description="«Старая» цена акции — показывается зачёркнутой.",
    )
    winback_enabled: bool = Field(
        default=True,
        validation_alias="WINBACK_ENABLED",
        description="Скидка на возвращение тем, у кого закончилась оплаченная подписка (см. offers_service).",
    )
    winback_discount_percent: Decimal = Field(default=Decimal("20"), ge=0, le=90, validation_alias="WINBACK_DISCOUNT_PERCENT")
    winback_min_days_after_expiry: int = Field(default=3, ge=0, le=365, validation_alias="WINBACK_MIN_DAYS_AFTER_EXPIRY")
    winback_max_days_after_expiry: int = Field(default=60, ge=1, le=3650, validation_alias="WINBACK_MAX_DAYS_AFTER_EXPIRY")
    winback_offer_days: int = Field(default=7, ge=1, le=90, validation_alias="WINBACK_OFFER_DAYS")
    winback_cooldown_days: int = Field(default=180, ge=1, le=3650, validation_alias="WINBACK_COOLDOWN_DAYS")
    landing_trial_promo_code: str = Field(
        default="TEST3",
        validation_alias="LANDING_TRIAL_PROMO_CODE",
        description="Промокод пробного периода, который лендинг показывает после входа (блок «3 дня бесплатно»). Пусто — блок скрыт.",
    )
    landing_trial_days: int = Field(
        default=3,
        ge=1,
        le=90,
        validation_alias="LANDING_TRIAL_DAYS",
        description="Сколько дней даёт промокод пробного периода (текст на лендинге).",
    )
    email_site_url: str = Field(
        default="https://my.flux-network.store",
        validation_alias="EMAIL_SITE_URL",
        description="Адрес сайта в письмах: ссылка в подвале, кнопки «Личный кабинет»/«Продлить», ссылка отписки. Пусто — PUBLIC_SITE_URL.",
    )
    email_support_bot_username: str = Field(
        default="flux_network_support_bot",
        validation_alias="EMAIL_SUPPORT_BOT_USERNAME",
        description="Telegram-бот поддержки (без @) — ссылка «Поддержка» в подвале писем. Пусто — SUPPORT_USERNAME.",
    )
    subscription_email_notify_enabled: bool = Field(
        default=True,
        validation_alias="SUBSCRIPTION_EMAIL_NOTIFY_ENABLED",
        description=(
            "Транзакционные письма на привязанную почту: чек о пополнении, продление подписки, "
            "напоминания за ~3 дня / ~6 часов до её окончания. "
            "Работает только если настроен SMTP_* (или RESEND_*) и у пользователя подтверждена почта."
        ),
    )
    billing_payg_subscription_days: int = Field(
        default=365,
        ge=30,
        le=3650,
        validation_alias="BILLING_PAYG_SUBSCRIPTION_DAYS",
        description="Срок PAYG-подписки в днях при выдаче/автопродлении (обычно 365).",
    )

    # Рефералы: бонус при /start по ссылке + процент с платежей приглашённого (пополнение, тариф/слот с баланса)
    referral_signup_bonus_rub: Decimal = Field(
        default=Decimal("15"),
        validation_alias="REFERRAL_SIGNUP_BONUS_RUB",
        description="Регистрация по реф-ссылке: столько ₽ на основной баланс и пригласившему, и приглашённому (0 = выкл.)",
    )
    referral_inviter_reward_rub_per_30_days: Decimal = Field(
        default=Decimal("0"),
        validation_alias=AliasChoices(
            "REFERRAL_INVITER_REWARD_RUB_PER_30_DAYS",
            "REFERRAL_INVITER_BONUS_RUB",
        ),
        description="Устарело (оставлено для совместимости): раньше фикс. ₽ за первую покупку тарифа; используйте REFERRAL_PAYMENT_PERCENT.",
    )
    referral_inviter_reward_days_per_30_days: int = Field(
        default=0,
        validation_alias=AliasChoices(
            "REFERRAL_INVITER_REWARD_DAYS_PER_30_DAYS",
            "REFERRAL_INVITER_BONUS_DAYS",
        ),
        description="Устарело: дни подписки рефереру за первую покупку друга (0 = выкл.).",
    )
    referral_payment_percent: Decimal = Field(
        default=Decimal("10"),
        validation_alias=AliasChoices("REFERRAL_PAYMENT_PERCENT", "REFERRAL_TOPUP_PERCENT"),
        description=(
            "Процент на баланс реферера от платежей приглашённого: пополнения (Platega и т.д.) "
            "и списания с баланса (тариф, доп. устройство). 0 = выкл."
        ),
    )

    maintenance_mode: bool = Field(default=False, validation_alias="MAINTENANCE_MODE")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    debug: bool = Field(default=False, validation_alias="DEBUG")
    web_admin_session_secret: str = Field(
        default="change-me-in-env",
        validation_alias="WEB_ADMIN_SESSION_SECRET",
        description="Секрет cookie-сессии web-admin",
    )
    web_admin_session_https_only: bool = Field(
        default=True,
        validation_alias="WEB_ADMIN_SESSION_HTTPS_ONLY",
        description="Cookie сессии web-admin только по HTTPS (Secure). Отключать только для локальной разработки по http.",
    )
    web_admin_github_logins_csv: str = Field(
        default="",
        validation_alias="WEB_ADMIN_GITHUB_LOGINS",
        description="Список GitHub login для входа в web-admin через запятую",
    )
    web_admin_github_client_id: str = Field(
        default="",
        validation_alias="WEB_ADMIN_GITHUB_CLIENT_ID",
        description="OAuth App Client ID для входа в web-admin через GitHub",
    )
    web_admin_github_client_secret: str = Field(
        default="",
        validation_alias="WEB_ADMIN_GITHUB_CLIENT_SECRET",
        description="OAuth App Client Secret для входа в web-admin через GitHub",
    )
    web_admin_github_redirect_uri: str = Field(
        default="",
        validation_alias="WEB_ADMIN_GITHUB_REDIRECT_URI",
        description="Полный callback URL GitHub OAuth (например https://admin.example.com/admin/login/github/callback)",
    )
    web_admin_google_redirect_uri: str = Field(
        default="",
        validation_alias="WEB_ADMIN_GOOGLE_REDIRECT_URI",
        description=(
            "Callback Google OAuth для web-admin (например https://weba.example.com/admin/login/google/callback). "
            "Client ID/Secret — те же SITE_GOOGLE_*. Пусто — {ADMIN_SITE_URL|PUBLIC_SITE_URL}/admin/login/google/callback."
        ),
    )
    web_admin_telegram_client_id: str = Field(
        default="",
        validation_alias="WEB_ADMIN_TELEGRAM_CLIENT_ID",
        description="Telegram OAuth/OpenID Client ID для входа в web-admin (из BotFather Web Login).",
    )
    web_admin_telegram_client_secret: str = Field(
        default="",
        validation_alias="WEB_ADMIN_TELEGRAM_CLIENT_SECRET",
        description="Telegram OAuth/OpenID Client Secret для входа в web-admin.",
    )
    web_admin_telegram_redirect_uri: str = Field(
        default="",
        validation_alias="WEB_ADMIN_TELEGRAM_REDIRECT_URI",
        description="Callback URL Telegram OAuth/OpenID (например https://admin.example.com/admin/login/telegram/widget).",
    )
    site_telegram_redirect_uri: str = Field(
        default="",
        validation_alias="SITE_TELEGRAM_REDIRECT_URI",
        description=(
            "Callback URL Telegram OAuth/OpenID для входа на клиентский сайт (например "
            "https://vpn.example.com/login/telegram/oauth-callback). Тот же client_id/secret, что у "
            "web-admin (WEB_ADMIN_TELEGRAM_CLIENT_ID/SECRET) — в BotFather Web Login этот redirect_uri "
            "нужно добавить как ещё один разрешённый, если провайдер требует точное совпадение."
        ),
    )
    site_google_client_id: str = Field(
        default="",
        validation_alias="SITE_GOOGLE_CLIENT_ID",
        description="Google OAuth Client ID (Google Cloud Console) для входа на сайт через Google.",
    )
    site_google_client_secret: str = Field(
        default="",
        validation_alias="SITE_GOOGLE_CLIENT_SECRET",
        description="Google OAuth Client Secret для входа на сайт через Google.",
    )
    site_google_redirect_uri: str = Field(
        default="",
        validation_alias="SITE_GOOGLE_REDIRECT_URI",
        description=(
            "Callback URL Google OAuth (например https://my.flux-network.store/login/google/callback). "
            "Должен быть добавлен в Google Cloud Console как Authorized redirect URI."
        ),
    )
    resend_api_key: str = Field(
        default="",
        validation_alias="RESEND_API_KEY",
        description="API-ключ Resend (resend.com) для отправки писем с кодом подтверждения почты.",
    )
    resend_from_email: str = Field(
        default="",
        validation_alias="RESEND_FROM_EMAIL",
        description="Адрес отправителя для писем с кодом (например noreply@mail.flux-network.store), домен должен быть подтверждён в Resend.",
    )
    smtp_host: str = Field(
        default="",
        validation_alias="SMTP_HOST",
        description="SMTP-сервер для отправки писем с кодом (например smtp.gmail.com, smtp.yandex.ru, smtp.mail.ru) — бесплатная альтернатива Resend, не требует подтверждения домена. Если задан — используется вместо Resend.",
    )
    smtp_port: int = Field(
        default=587,
        validation_alias="SMTP_PORT",
        description="Порт SMTP (587 — STARTTLS, обычно подходит для Gmail/Yandex/Mail.ru).",
    )
    smtp_user: str = Field(
        default="",
        validation_alias="SMTP_USER",
        description="Логин SMTP — обычно полный адрес почты (например you@gmail.com).",
    )
    smtp_password: str = Field(
        default="",
        validation_alias="SMTP_PASSWORD",
        description="Пароль приложения SMTP (НЕ обычный пароль от почты — см. инструкцию для Gmail/Yandex).",
    )
    smtp_from_email: str = Field(
        default="",
        validation_alias="SMTP_FROM_EMAIL",
        description="Адрес отправителя в письмах. Если пусто — берётся SMTP_USER.",
    )
    public_site_url: str | None = Field(
        default=None,
        validation_alias="PUBLIC_SITE_URL",
        description=(
            "Публичный HTTPS-origin клиентского сайта (например https://vpn.example.com) — лендинг, вход, "
            "личный кабинет, реферальные ссылки, кнопка Mini App в боте; без слэша на конце"
        ),
    )
    admin_site_url: str | None = Field(
        default=None,
        validation_alias="ADMIN_SITE_URL",
        description=(
            "Публичный HTTPS-origin веб-админки (например https://weba.example.com) — используется для "
            "ссылок на /admin/... в админ-логе Telegram и темах тикетов. Если не задан — берётся PUBLIC_SITE_URL "
            "(для обратной совместимости, если сайт и админка на одном домене). Без слэша на конце."
        ),
    )
    miniapp_url: str | None = Field(
        default=None,
        validation_alias="MINIAPP_URL",
        description=(
            "Полный HTTPS-URL мини-аппа для кнопки в боте (например https://miniapp.example.com/my). "
            "Если не задан — используется {PUBLIC_SITE_URL}/my. Без слэша на конце."
        ),
    )
    admin_panel_title: str = Field(
        default="Remna",
        validation_alias="ADMIN_PANEL_TITLE",
        description="Название в шапке web-admin (боковое меню)",
    )
    admin_panel_logo_url: str | None = Field(
        default=None,
        validation_alias="ADMIN_PANEL_LOGO_URL",
        description="URL картинки-логотипа в шапке web-admin и favicon страниц",
    )
    web_admin_profile_display_name: str | None = Field(
        default=None,
        validation_alias="WEB_ADMIN_PROFILE_DISPLAY_NAME",
        description="Подпись на карточке «Мой профиль» в web-admin (если пусто — имя из Telegram/GitHub)",
    )
    admin_background_source: str = Field(
        default="default",
        validation_alias="ADMIN_BACKGROUND_SOURCE",
        description="Источник фона админки: default | url | asset",
    )
    admin_background_url: str | None = Field(
        default=None,
        validation_alias="ADMIN_BACKGROUND_URL",
        description="URL фонового изображения для админки (если ADMIN_BACKGROUND_SOURCE=url)",
    )
    admin_background_asset: str | None = Field(
        default=None,
        validation_alias="ADMIN_BACKGROUND_ASSET",
        description="Имя файла из /assets для фона админки (если ADMIN_BACKGROUND_SOURCE=asset)",
    )

    backup_enabled: bool = Field(
        default=False,
        validation_alias="BACKUP_ENABLED",
        description="Ежедневный pg_dump и отправка в админ-чат (тема BACKUPS), если задан ADMIN_LOG_CHAT_ID",
    )
    backup_hour_utc: int = Field(
        default=6,
        ge=0,
        le=23,
        validation_alias="BACKUP_HOUR_UTC",
        description="Час UTC для ежедневного бэкапа PostgreSQL",
    )
    backup_timezone: str = Field(
        default="UTC",
        validation_alias="BACKUP_TIMEZONE",
        description="Часовой пояс ежедневного бэкапа (например Europe/Moscow)",
    )
    backup_hour_local: int | None = Field(
        default=None,
        ge=0,
        le=23,
        validation_alias="BACKUP_HOUR_LOCAL",
        description="Час в BACKUP_TIMEZONE. Если задан, имеет приоритет над BACKUP_HOUR_UTC",
    )
    backup_max_telegram_mb: float = Field(
        default=45.0,
        ge=1.0,
        le=49.0,
        validation_alias="BACKUP_MAX_TELEGRAM_MB",
        description="Максимальный размер файла для отправки в Telegram (лимит бота ~50 МБ)",
    )
    backup_pg_dump_bin: str | None = Field(
        default=None,
        validation_alias="BACKUP_PG_DUMP_BIN",
        description=(
            "Полный путь к pg_dump той же major-версии, что и сервер PostgreSQL "
            "(иначе pg_dump откажется при несовпадении версий)"
        ),
    )
    backup_project_dir: str = Field(
        default="/app",
        validation_alias="BACKUP_PROJECT_DIR",
        description="Папка проекта, которую нужно упаковать вместе с бэкапом БД",
    )
    backup_project_name: str | None = Field(
        default=None,
        validation_alias="BACKUP_PROJECT_NAME",
        description="Имя проекта для названий архивов (если пусто — берётся имя папки BACKUP_PROJECT_DIR)",
    )
    backup_local_dir: str = Field(
        default="/opt/remna-bot/backups",
        validation_alias="BACKUP_LOCAL_DIR",
        description="Папка для локального хранения архивов бэкапа на сервере",
    )
    backup_local_retention_days: int = Field(
        default=7,
        ge=1,
        le=365,
        validation_alias="BACKUP_LOCAL_RETENTION_DAYS",
        description="Сколько дней хранить локальные бэкапы",
    )

    # CryptoBot (@CryptoBot / Crypto Pay API)
    cryptobot_token: str = Field(default="", validation_alias="CRYPTOBOT_TOKEN")
    cryptobot_stub: bool = Field(default=False, validation_alias="CRYPTOBOT_STUB")

    # Platega.io (реальный API: POST /transaction/process, заголовки X-MerchantId / X-Secret)
    platega_merchant_id: str = Field(
        default="",
        validation_alias=AliasChoices("PLATEGA_MERCHANT_ID", "PLATEGA_SHOP_ID"),
        description="UUID мерчанта (как в кабинете Platega)",
    )
    platega_secret_key: str = Field(default="", validation_alias="PLATEGA_SECRET_KEY")
    platega_webhook_secret: str = Field(
        default="",
        validation_alias="PLATEGA_WEBHOOK_SECRET",
        description="Опционально: отдельный секрет; иначе сверяем X-Secret с PLATEGA_SECRET_KEY",
    )
    platega_api_base_url: str = Field(
        default="https://app.platega.io",
        validation_alias="PLATEGA_API_BASE_URL",
        description="Базовый URL из docs.platega.io; при сбоях можно указать https://api.platega.io",
    )
    platega_payment_methods_csv: str = Field(
        default="2",
        validation_alias=AliasChoices("PLATEGA_PAYMENT_METHODS", "PLATEGA_PAYMENT_METHOD"),
    )
    platega_success_url: str = Field(default="", validation_alias="PLATEGA_SUCCESS_URL")
    platega_fail_url: str = Field(default="", validation_alias="PLATEGA_FAIL_URL")
    platega_stub: bool = Field(default=False, validation_alias="PLATEGA_STUB")
    platega_skip_webhook_auth: bool = Field(
        default=False,
        validation_alias="PLATEGA_SKIP_WEBHOOK_AUTH",
        description="Только для отладки: не проверять X-MerchantId/X-Secret на вебхуке",
    )
    platega_payer_chooses_method: bool = Field(
        default=False,
        validation_alias="PLATEGA_PAYER_CHOOSES_METHOD",
        description=(
            "Создавать ссылку без фиксированного способа: плательщик выбирает на стороне Platega (POST /v2/transaction/process). "
            "В этом режиме PLATEGA_PAYMENT_METHODS в запрос не уходит."
        ),
    )

    @property
    def platega_payment_methods(self) -> list[int]:
        out: list[int] = []
        raw = (self.platega_payment_methods_csv or "").strip()
        for part in raw.split(","):
            val = part.strip()
            if not val:
                continue
            try:
                method_id = int(val)
            except ValueError:
                continue
            if method_id not in out:
                out.append(method_id)
        return out or [2]

    @property
    def platega_payment_method(self) -> int:
        return self.platega_payment_methods[0]

    @field_validator("admin_log_chat_id", mode="before")
    @classmethod
    def _empty_admin_chat(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return v

    @field_validator(
        "admin_log_topic_id",
        "admin_log_topic_general",
        "admin_log_topic_payments",
        "admin_log_topic_users",
        "admin_log_topic_trials",
        "admin_log_topic_bonuses",
        "admin_log_topic_subscriptions",
        "admin_log_topic_promo",
        "admin_log_topic_devices",
        "admin_log_topic_support",
        "admin_log_topic_login",
        "admin_log_topic_backups",
        "admin_log_topic_reports",
        "admin_log_topic_boot",
        "admin_log_topic_channel",
        "admin_log_topic_errors",
        "admin_log_topic_fraud_suspicion",
        "admin_log_topic_fraud_autoblock",
        mode="before",
    )
    @classmethod
    def _empty_admin_topic(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return int(v)

    @field_validator("superadmin_telegram_id", mode="before")
    @classmethod
    def _empty_superadmin_telegram_id(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return v

    @field_validator("admin_telegram_id", mode="before")
    @classmethod
    def _empty_admin_telegram_id(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return v

    @field_validator("bot_section_photo_path", "bot_section_photo_url", "remnawave_public_url", "public_site_url", "admin_site_url", mode="before")
    @classmethod
    def _empty_photo_fields(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_superadmin_telegram_id(self) -> int | None:
        """SUPERADMIN_TELEGRAM_ID или legacy ADMIN_TELEGRAM_ID."""
        if self.superadmin_telegram_id is not None:
            return int(self.superadmin_telegram_id)
        if self.admin_telegram_id is not None:
            return int(self.admin_telegram_id)
        return None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def admin_telegram_ids(self) -> list[int]:
        """Итоговый список id админов (legacy + супер-админ)."""
        out: list[int] = []
        raw = (self.admin_telegram_ids_csv or "").strip()
        if raw:
            for part in raw.replace(";", ",").split(","):
                p = part.strip()
                if p.isdigit() or (p.startswith("-") and p[1:].isdigit()):
                    out.append(int(p))
        if self.admin_telegram_id is not None:
            out.append(int(self.admin_telegram_id))
        if self.superadmin_telegram_id is not None:
            out.append(int(self.superadmin_telegram_id))
        return list(dict.fromkeys(out))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def web_admin_github_logins(self) -> list[str]:
        raw = (self.web_admin_github_logins_csv or "").strip()
        if not raw:
            return []
        out: list[str] = []
        for part in raw.replace(";", ",").split(","):
            login = part.strip().lstrip("@")
            if login:
                out.append(login)
        return list(dict.fromkeys(out))

    def admin_log_thread_for(self, topic: AdminLogTopic) -> int | None:
        m: dict[AdminLogTopic, int | None] = {
            AdminLogTopic.GENERAL: self.admin_log_topic_general,
            AdminLogTopic.PAYMENTS: self.admin_log_topic_payments,
            AdminLogTopic.USERS: self.admin_log_topic_users,
            AdminLogTopic.TRIALS: self.admin_log_topic_trials,
            AdminLogTopic.BONUSES: self.admin_log_topic_bonuses,
            AdminLogTopic.SUBSCRIPTIONS: self.admin_log_topic_subscriptions,
            AdminLogTopic.PROMO: self.admin_log_topic_promo,
            AdminLogTopic.DEVICES: self.admin_log_topic_devices,
            AdminLogTopic.SUPPORT: self.admin_log_topic_support,
            AdminLogTopic.LOGIN: self.admin_log_topic_login,
            AdminLogTopic.BACKUPS: self.admin_log_topic_backups,
            AdminLogTopic.REPORTS: self.admin_log_topic_reports,
            AdminLogTopic.BOOT: self.admin_log_topic_boot,
            AdminLogTopic.CHANNEL: self.admin_log_topic_channel,
            AdminLogTopic.ERRORS: self.admin_log_topic_errors,
            AdminLogTopic.FRAUD_SUSPICION: self.admin_log_topic_fraud_suspicion,
            AdminLogTopic.FRAUD_AUTOBLOCK: self.admin_log_topic_fraud_autoblock,
        }
        tid = m.get(topic)
        if tid is not None:
            return tid
        if self.admin_log_topic_general is not None:
            return self.admin_log_topic_general
        return self.admin_log_topic_id

    @model_validator(mode="after")
    def _validate_remnawave(self) -> "Settings":
        if not self.remnawave_stub:
            if not (self.remnawave_api_token or "").strip():
                raise ValueError("Задайте REMNAWAVE_API_TOKEN или REMNAWAVE_STUB=true")
        return self

    @model_validator(mode="after")
    def _validate_telegram_webhook(self) -> "Settings":
        if self.telegram_webhook_enabled:
            if not (self.telegram_webhook_url or "").strip():
                raise ValueError("TELEGRAM_WEBHOOK_ENABLED=true требует непустой TELEGRAM_WEBHOOK_URL (HTTPS)")
            if len((self.telegram_webhook_secret or "").strip()) < 8:
                raise ValueError("TELEGRAM_WEBHOOK_SECRET: не менее 8 символов при включённом вебхуке")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
