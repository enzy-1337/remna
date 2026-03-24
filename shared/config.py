"""Общие настройки приложения (Pydantic Settings)."""

from decimal import Decimal
from functools import lru_cache

from pydantic import AliasChoices, Field, computed_field, field_validator, model_validator

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
    support_username: str | None = Field(
        default=None,
        validation_alias="SUPPORT_USERNAME",
        description="Поддержка: username без @",
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
    instruction_windows_url: str | None = Field(
        default=None,
        validation_alias="INSTRUCTION_WINDOWS_URL",
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
    admin_log_topic_backups: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_BACKUPS")
    admin_log_topic_reports: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_REPORTS")
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
        description="Один Telegram user id админа (альтернатива списку ADMIN_TELEGRAM_IDS)",
    )
    # В .env строка «1,2,3» или пусто — не list[int] (иначе pydantic-settings ждёт JSON и падает на "")
    admin_telegram_ids_csv: str = Field(
        default="",
        validation_alias="ADMIN_TELEGRAM_IDS",
        description="Несколько Telegram user id через запятую",
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
    )
    remnawave_cookie: str | None = Field(
        default=None,
        validation_alias="REMNAWAVE_COOKIE",
        description="Cookie для nginx reverse-proxy: либо значение (WbYWpixX), либо целиком NAME=VALUE (как в docs).",
    )
    remnawave_request_timeout: float = Field(default=10.0, validation_alias="REMNAWAVE_REQUEST_TIMEOUT")
    remnawave_sync_enabled: bool = Field(default=True, validation_alias="REMNAWAVE_SYNC_ENABLED")
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

    # Триал
    trial_duration_days: int = Field(default=3, validation_alias="TRIAL_DURATION_DAYS")
    trial_traffic_gb: int = Field(default=1, validation_alias="TRIAL_TRAFFIC_GB")

    # Подписка / устройства (шаг 8)
    extra_device_price_rub: Decimal = Field(default=Decimal("65"), validation_alias="EXTRA_DEVICE_PRICE_RUB")
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

    # Рефералы: бонус при /start по ссылке + награда пригласившему за первую покупку друга (пропорционально сроку)
    referral_signup_bonus_rub: Decimal = Field(
        default=Decimal("15"),
        validation_alias="REFERRAL_SIGNUP_BONUS_RUB",
        description="Регистрация по реф-ссылке: столько ₽ на основной баланс и пригласившему, и приглашённому (0 = выкл.)",
    )
    referral_inviter_reward_rub_per_30_days: Decimal = Field(
        default=Decimal("15"),
        validation_alias=AliasChoices(
            "REFERRAL_INVITER_REWARD_RUB_PER_30_DAYS",
            "REFERRAL_INVITER_BONUS_RUB",
        ),
        description="Первая платная покупка приглашённого: ₽ пригласившему на каждые 30 дн. купленного тарифа (0 = выкл.)",
    )
    referral_inviter_reward_days_per_30_days: int = Field(
        default=5,
        validation_alias=AliasChoices(
            "REFERRAL_INVITER_REWARD_DAYS_PER_30_DAYS",
            "REFERRAL_INVITER_BONUS_DAYS",
        ),
        description="Там же: дней к активной подписке пригласившего на каждые 30 дн. периода (0 = выкл.)",
    )

    maintenance_mode: bool = Field(default=False, validation_alias="MAINTENANCE_MODE")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    debug: bool = Field(default=False, validation_alias="DEBUG")
    web_admin_session_secret: str = Field(
        default="change-me-in-env",
        validation_alias="WEB_ADMIN_SESSION_SECRET",
        description="Секрет cookie-сессии web-admin",
    )
    web_admin_github_logins_csv: str = Field(
        default="",
        validation_alias="WEB_ADMIN_GITHUB_LOGINS",
        description="Список GitHub login для входа в web-admin через запятую",
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
    platega_payment_method: int = Field(default=2, validation_alias="PLATEGA_PAYMENT_METHOD")
    platega_success_url: str = Field(default="", validation_alias="PLATEGA_SUCCESS_URL")
    platega_fail_url: str = Field(default="", validation_alias="PLATEGA_FAIL_URL")
    platega_stub: bool = Field(default=False, validation_alias="PLATEGA_STUB")
    platega_skip_webhook_auth: bool = Field(
        default=False,
        validation_alias="PLATEGA_SKIP_WEBHOOK_AUTH",
        description="Только для отладки: не проверять X-MerchantId/X-Secret на вебхуке",
    )

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
        "admin_log_topic_backups",
        "admin_log_topic_reports",
        mode="before",
    )
    @classmethod
    def _empty_admin_topic(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return int(v)

    @field_validator("admin_telegram_id", mode="before")
    @classmethod
    def _empty_admin_telegram_id(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return v

    @field_validator("bot_section_photo_path", "bot_section_photo_url", "remnawave_public_url", mode="before")
    @classmethod
    def _empty_photo_fields(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def admin_telegram_ids(self) -> list[int]:
        """Итоговый список id админов: ADMIN_TELEGRAM_IDS + ADMIN_TELEGRAM_ID."""
        out: list[int] = []
        raw = (self.admin_telegram_ids_csv or "").strip()
        if raw:
            for part in raw.replace(";", ",").split(","):
                p = part.strip()
                if p.isdigit() or (p.startswith("-") and p[1:].isdigit()):
                    out.append(int(p))
        if self.admin_telegram_id is not None:
            out = list(dict.fromkeys([*out, self.admin_telegram_id]))
        return out

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
            AdminLogTopic.BACKUPS: self.admin_log_topic_backups,
            AdminLogTopic.REPORTS: self.admin_log_topic_reports,
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
