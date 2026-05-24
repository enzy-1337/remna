"""Поиск медиа: музыка, позже Pinterest для Reels."""

from shared.services.media_search.download import download_track_to_mp3
from shared.services.media_search.identify import (
    extract_first_url,
    identify_audio_file,
    query_from_telegram_video_file,
    query_from_video_url,
)
from shared.services.media_search.search import search_music_all
from shared.services.media_search.session_store import (
    get_track_from_session,
    load_search_session,
    new_session_id,
    save_search_session,
)
from shared.services.media_search.types import MediaTrack

__all__ = [
    "MediaTrack",
    "download_track_to_mp3",
    "extract_first_url",
    "get_track_from_session",
    "identify_audio_file",
    "load_search_session",
    "new_session_id",
    "query_from_telegram_video_file",
    "query_from_video_url",
    "save_search_session",
    "search_music_all",
]
