from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DownloaderSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    downloader_bot_token: str = Field(default="", validation_alias="DOWNLOADER_BOT_TOKEN")
    downloader_forum_chat_id: int | None = Field(default=None, validation_alias="DOWNLOADER_FORUM_CHAT_ID")
    downloader_max_file_mb: int = Field(default=0, ge=0, validation_alias="DOWNLOADER_MAX_FILE_MB")
    downloader_max_duration_sec: int = Field(default=0, ge=0, validation_alias="DOWNLOADER_MAX_DURATION_SEC")
    bot_username: str | None = Field(default=None, validation_alias="BOT_USERNAME")
    database_url: str = Field(..., validation_alias="DATABASE_URL")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    admin_log_chat_id: str | int | None = Field(default=None, validation_alias="ADMIN_LOG_CHAT_ID")
    admin_log_topic_boot: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_BOOT")
    admin_log_topic_id: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_ID")


@lru_cache
def get_downloader_settings() -> DownloaderSettings:
    return DownloaderSettings()
