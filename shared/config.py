"""Общие настройки приложения (Pydantic Settings)."""

from decimal import Decimal
from functools import lru_cache

from pydantic import AliasChoices, Field, computed_field, field_validator, model_validator
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

    # Рефералы (шаг 9): награда пригласившему за первую платную покупку приглашённого
    referral_inviter_bonus_rub: Decimal = Field(
        default=Decimal("0"),
        validation_alias="REFERRAL_INVITER_BONUS_RUB",
        description="0 = выключено",
    )
    referral_inviter_bonus_days: int = Field(
        default=0,
        validation_alias="REFERRAL_INVITER_BONUS_DAYS",
        description="Дней к активной подписке пригласившего (0 = не начислять)",
    )
    referral_signup_bonus_rub: Decimal = Field(
        default=Decimal("0"),
        validation_alias="REFERRAL_SIGNUP_BONUS_RUB",
        description="Однократно пригласившему при регистрации друга по ссылке (0 = выкл.)",
    )

    maintenance_mode: bool = Field(default=False, validation_alias="MAINTENANCE_MODE")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    debug: bool = Field(default=False, validation_alias="DEBUG")

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
        default="https://api.platega.io",
        validation_alias="PLATEGA_API_BASE_URL",
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

    @field_validator("admin_log_topic_id", mode="before")
    @classmethod
    def _empty_admin_topic(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return v

    @field_validator("admin_telegram_id", mode="before")
    @classmethod
    def _empty_admin_telegram_id(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return v

    @field_validator("bot_section_photo_path", "bot_section_photo_url", mode="before")
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

    @model_validator(mode="after")
    def _validate_remnawave(self) -> "Settings":
        if not self.remnawave_stub:
            if not (self.remnawave_api_token or "").strip():
                raise ValueError("Задайте REMNAWAVE_API_TOKEN или REMNAWAVE_STUB=true")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
