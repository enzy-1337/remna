from __future__ import annotations

import logging

from tickets.config import config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("tickets")
    log.info(
        "Tickets config loaded: SUPPORT_GROUP_ID=%s REMINDER_HOURS=%s AUTO_CLOSE_DAYS=%s ADMIN_IDS=%s",
        config.support_group_id,
        config.reminder_hours,
        config.auto_close_days,
        config.admin_ids,
    )


if __name__ == "__main__":
    main()

