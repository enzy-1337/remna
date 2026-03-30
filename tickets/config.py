from __future__ import annotations

import os
from dataclasses import dataclass, field


def _parse_int_csv(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in raw.split(","):
        s = (part or "").strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except ValueError:
            continue
    return out


@dataclass(frozen=True)
class TicketsConfig:
    bot_token: str = ""
    support_group_id: int = 0
    reminder_hours: int = 6
    auto_close_days: int = 3
    admin_ids: list[int] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "TicketsConfig":
        def _int_env(name: str, default: int) -> int:
            raw = os.getenv(name, "")
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        support_group_id = _int_env("SUPPORT_GROUP_ID", 0)
        reminder_hours = _int_env("REMINDER_HOURS", 6)
        auto_close_days = _int_env("AUTO_CLOSE_DAYS", 3)
        admin_ids = _parse_int_csv(os.getenv("ADMIN_IDS"))
        return cls(
            bot_token=(os.getenv("TICKETS_BOT_TOKEN", "") or "").strip(),
            support_group_id=support_group_id,
            reminder_hours=reminder_hours,
            auto_close_days=auto_close_days,
            admin_ids=admin_ids,
        )


config = TicketsConfig.from_env()

