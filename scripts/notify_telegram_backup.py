#!/usr/bin/env python3
"""
Отправка сообщения о бэкапе в тему ADMIN_LOG_TOPIC_BACKUPS (без Markdown).

Примеры:
  python scripts/notify_telegram_backup.py < backup_msg.txt
  python scripts/notify_telegram_backup.py --file message.txt
  python scripts/notify_telegram_backup.py --text "💾 #backup_success\\n✅ Готово"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


async def _run() -> int:
    from shared.config import get_settings
    from shared.services.admin_log_topics import AdminLogTopic
    from shared.services.admin_notify import notify_admin_plain

    p = argparse.ArgumentParser(description="Уведомление о бэкапе в Telegram (тема BACKUPS)")
    p.add_argument("--text", help="Текст (\\n как перевод строки)")
    p.add_argument("--file", help="Путь к файлу с текстом")
    args = p.parse_args()

    if args.text is not None:
        body = args.text.replace("\\n", "\n")
    elif args.file:
        body = Path(args.file).read_text(encoding="utf-8")
    else:
        body = sys.stdin.read()

    body = body.strip()
    if not body:
        print("Пустой текст: укажите stdin, --file или --text", file=sys.stderr)
        return 1

    settings = get_settings()
    ok = await notify_admin_plain(
        settings,
        text=body,
        topic=AdminLogTopic.BACKUPS,
        event_type="backup",
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
