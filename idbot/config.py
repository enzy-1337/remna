from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class IdBotSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    idbot_bot_token: str = Field(default="", validation_alias="IDBOT_BOT_TOKEN")
    bot_username: str | None = Field(default=None, validation_alias="BOT_USERNAME")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    admin_log_chat_id: str | int | None = Field(default=None, validation_alias="ADMIN_LOG_CHAT_ID")
    admin_log_topic_boot: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_BOOT")
    admin_log_topic_id: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_ID")


@lru_cache
def get_idbot_settings() -> IdBotSettings:
    return IdBotSettings()
