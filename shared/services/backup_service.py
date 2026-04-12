"""Ежедневный pg_dump PostgreSQL и отправка в админ-чат (тема BACKUPS)."""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from shared.config import Settings
from shared.services.admin_log_topics import AdminLogTopic
from shared.services.admin_notify import notify_admin_document, notify_admin_plain

logger = logging.getLogger(__name__)


def _parse_pg_url(database_url: str) -> dict[str, str | int | None]:
    raw = database_url.strip()
    if "postgresql+asyncpg://" in raw:
        raw = raw.replace("postgresql+asyncpg://", "postgresql://", 1)
    elif raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql://", 1)
    elif not raw.startswith("postgresql://"):
        raise ValueError("Ожидается DATABASE_URL на PostgreSQL")
    p = urlparse(raw)
    host = p.hostname or "localhost"
    port = p.port or 5432
    user = unquote(p.username or "")
    password = unquote(p.password or "")
    dbname = (p.path or "").lstrip("/").split("/")[0]
    if not dbname:
        raise ValueError("В DATABASE_URL нет имени базы")
    q = parse_qs(p.query)
    sslmode = (q.get("sslmode") or [None])[0]
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "dbname": dbname,
        "sslmode": sslmode,
    }


async def run_daily_backup(settings: Settings) -> None:
    if not settings.backup_enabled:
        return
    try:
        params = _parse_pg_url(settings.database_url)
    except ValueError as e:
        logger.warning("backup: %s", e)
        await notify_admin_plain(
            settings,
            text=f"💾 Бэкап: пропуск — {e}",
            topic=AdminLogTopic.BACKUPS,
            event_type="backup_skip",
        )
        return

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    fd, sql_path = tempfile.mkstemp(prefix=f"remna_pg_{ts}_", suffix=".sql")
    os.close(fd)
    sql_p = Path(sql_path)
    gz_path = Path(sql_path + ".gz")

    env = os.environ.copy()
    env["PGPASSWORD"] = str(params["password"])
    sslmode = params.get("sslmode")
    if sslmode:
        env["PGSSLMODE"] = str(sslmode)

    pg_dump_exe = (settings.backup_pg_dump_bin or "").strip() or "pg_dump"
    cmd = [
        pg_dump_exe,
        "-h",
        str(params["host"]),
        "-p",
        str(params["port"]),
        "-U",
        str(params["user"]),
        "-Fp",
        "--no-owner",
        "-f",
        str(sql_p),
        str(params["dbname"]),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err_b = await asyncio.wait_for(proc.communicate(), timeout=3600.0)
    except FileNotFoundError:
        await notify_admin_plain(
            settings,
            text=(
                "💾 Бэкап: не найден pg_dump "
                f"({pg_dump_exe!r}; задайте BACKUP_PG_DUMP_BIN или установите клиент в PATH)."
            ),
            topic=AdminLogTopic.BACKUPS,
            event_type="backup_error",
        )
        if sql_p.exists():
            sql_p.unlink(missing_ok=True)
        return
    except asyncio.TimeoutError:
        await notify_admin_plain(
            settings,
            text="💾 Бэкап: pg_dump превысил таймаут (1 ч).",
            topic=AdminLogTopic.BACKUPS,
            event_type="backup_error",
        )
        if sql_p.exists():
            sql_p.unlink(missing_ok=True)
        return
    except Exception:
        logger.exception("backup: pg_dump failed")
        if sql_p.exists():
            sql_p.unlink(missing_ok=True)
        raise

    if proc.returncode != 0:
        err = (err_b or b"").decode("utf-8", errors="replace")[:3500]
        await notify_admin_plain(
            settings,
            text=f"💾 Бэкап: pg_dump завершился с кодом {proc.returncode}\n{err}",
            topic=AdminLogTopic.BACKUPS,
            event_type="backup_error",
        )
        sql_p.unlink(missing_ok=True)
        return

    try:
        with sql_p.open("rb") as f_in:
            with gzip.open(gz_path, "wb", compresslevel=9) as f_out:
                f_out.writelines(f_in)
    finally:
        sql_p.unlink(missing_ok=True)

    size = gz_path.stat().st_size
    max_bytes = int(settings.backup_max_telegram_mb * 1024 * 1024)
    mb = size / (1024 * 1024)
    fname = f"remna_pg_{ts}.sql.gz"

    if size > max_bytes:
        gz_path.unlink(missing_ok=True)
        await notify_admin_plain(
            settings,
            text=(
                f"💾 Бэкап PostgreSQL создан, но файл слишком большой для Telegram "
                f"({mb:.1f} МБ > {settings.backup_max_telegram_mb:.0f} МБ). "
                "Настройте внешнее хранилище или cron с pg_dump на сервере."
            ),
            topic=AdminLogTopic.BACKUPS,
            event_type="backup_too_large",
        )
        return

    ok = await notify_admin_document(
        settings,
        document_path=str(gz_path),
        caption=f"💾 PostgreSQL {params['dbname']} · {mb:.2f} МБ · {ts} UTC",
        topic=AdminLogTopic.BACKUPS,
        event_type="backup",
    )
    gz_path.unlink(missing_ok=True)
    if not ok:
        logger.warning("backup: отправка файла в Telegram не удалась")
