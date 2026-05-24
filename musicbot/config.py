from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.bot_cta import DEFAULT_BOT_CTA_LABEL


class MusicBotSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    music_bot_token: str = Field(default="", validation_alias="MUSIC_BOT_TOKEN")
    music_forum_chat_id: int | None = Field(default=None, validation_alias="MUSIC_FORUM_CHAT_ID")
    music_results_per_page: int = Field(default=8, ge=1, le=10, validation_alias="MUSIC_RESULTS_PER_PAGE")
    music_max_pages: int = Field(default=10, ge=1, le=10, validation_alias="MUSIC_MAX_PAGES")
    vk_access_token: str = Field(default="", validation_alias="VK_ACCESS_TOKEN")
    spotify_client_id: str = Field(default="", validation_alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: str = Field(default="", validation_alias="SPOTIFY_CLIENT_SECRET")
    yandex_music_token: str = Field(default="", validation_alias="YANDEX_MUSIC_TOKEN")
    bot_username: str | None = Field(default=None, validation_alias="BOT_USERNAME")
    bot_cta_label: str = Field(default=DEFAULT_BOT_CTA_LABEL, validation_alias="BOT_CTA_LABEL")
    database_url: str = Field(..., validation_alias="DATABASE_URL")
    redis_url: str = Field(default="redis://localhost:6379/0", validation_alias="REDIS_URL")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    admin_log_chat_id: str | int | None = Field(default=None, validation_alias="ADMIN_LOG_CHAT_ID")
    admin_log_topic_boot: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_BOOT")
    admin_log_topic_id: int | None = Field(default=None, validation_alias="ADMIN_LOG_TOPIC_ID")


@lru_cache
def get_musicbot_settings() -> MusicBotSettings:
    return MusicBotSettings()
