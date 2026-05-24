"""Общие типы для поиска медиа (музыка, позже Pinterest для Reels)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


SOURCE_LABELS: dict[str, str] = {
    "vk": "VK",
    "yt": "YT",
    "sc": "SC",
    "sf": "SF",
    "ya": "YA",
    "pi": "PI",
}


@dataclass(slots=True)
class MediaTrack:
    source: str
    title: str
    artist: str
    duration_sec: int | None = None
    url: str | None = None
    source_id: str | None = None
    extra: dict[str, Any] | None = None

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source.upper()[:2])

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["source_label"] = self.source_label
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MediaTrack:
        return cls(
            source=str(data.get("source") or "yt"),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
            duration_sec=data.get("duration_sec"),
            url=data.get("url"),
            source_id=data.get("source_id"),
            extra=data.get("extra") if isinstance(data.get("extra"), dict) else None,
        )

    def display_line(self, index: int) -> str:
        dur = _fmt_duration(self.duration_sec)
        artist = (self.artist or "Unknown").strip()
        title = (self.title or "—").strip()
        return f"{index}. [{self.source_label}] {artist} — {title} {dur}"


def _fmt_duration(sec: int | None) -> str:
    if sec is None or sec <= 0:
        return "??:??"
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"
