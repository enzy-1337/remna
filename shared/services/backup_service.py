"""Ежедневный pg_dump PostgreSQL и отправка в админ-чат (тема BACKUPS)."""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

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


async def _pg_dump_version(pg_dump_exe: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            pg_dump_exe,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        txt = (out_b or b"").decode("utf-8", errors="replace").strip()
        return txt or "unknown"
    except Exception:
        return "unknown"


def _backup_zone(settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo((settings.backup_timezone or "Europe/Moscow").strip() or "Europe/Moscow")
    except Exception:
        return ZoneInfo("Europe/Moscow")


def _backup_zone_label(settings: Settings) -> str:
    tz = (settings.backup_timezone or "Europe/Moscow").strip() or "Europe/Moscow"
    return "МСК" if tz == "Europe/Moscow" else tz


def _project_dir_and_name(settings: Settings) -> tuple[Path, str]:
    project_dir = Path((settings.backup_project_dir or "/app").strip() or "/app").resolve()
    project_name = (settings.backup_project_name or "").strip() or project_dir.name or "project"
    return project_dir, project_name


async def run_backup_once(settings: Settings, *, notify: bool = True) -> tuple[bool, str]:
    if not settings.backup_enabled:
        return False, "Бэкап отключён (BACKUP_ENABLED=false)."
    try:
        params = _parse_pg_url(settings.database_url)
    except ValueError as e:
        logger.warning("backup: %s", e)
        if notify:
            await notify_admin_plain(
                settings,
                text=f"💾 Бэкап: пропуск — {e}",
                topic=AdminLogTopic.BACKUPS,
                event_type="backup_skip",
            )
        return False, str(e)

    zone = _backup_zone(settings)
    zone_label = _backup_zone_label(settings)
    now_local = datetime.now(UTC).astimezone(zone)
    stamp_hms = now_local.strftime("%H_%M_%S")
    stamp_dmy = now_local.strftime("%d-%m-%Y")
    stamp_display = now_local.strftime(f"%H:%M:%S %d.%m.%Y {zone_label}")
    dbname = str(params["dbname"])
    project_dir, project_name = _project_dir_and_name(settings)

    tmp_dir = Path(tempfile.mkdtemp(prefix="remna_backup_"))
    sql_p = tmp_dir / f"dump_{dbname}_{stamp_hms}_{stamp_dmy}.sql"
    gz_path = tmp_dir / f"{sql_p.name}.gz"
    dir_archive = tmp_dir / f"{project_name}_dir_{stamp_hms}_{stamp_dmy}.tar.gz"
    final_archive = tmp_dir / f"{project_name}_backup_{stamp_hms}_{stamp_dmy}.tar.gz"

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
        msg = (
            "Не найден pg_dump "
            f"({pg_dump_exe!r}; задайте BACKUP_PG_DUMP_BIN или установите клиент в PATH)."
        )
        if notify:
            await notify_admin_plain(
                settings,
                text=f"💾 Бэкап: {msg}",
                topic=AdminLogTopic.BACKUPS,
                event_type="backup_error",
            )
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False, msg
    except asyncio.TimeoutError:
        msg = "pg_dump превысил таймаут (1 ч)."
        if notify:
            await notify_admin_plain(
                settings,
                text=f"💾 Бэкап: {msg}",
                topic=AdminLogTopic.BACKUPS,
                event_type="backup_error",
            )
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False, msg
    except Exception:
        logger.exception("backup: pg_dump failed")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    if proc.returncode != 0:
        err = (err_b or b"").decode("utf-8", errors="replace")[:3500]
        extra = ""
        if "server version mismatch" in err.lower():
            cur = await _pg_dump_version(pg_dump_exe)
            extra = (
                "\n\nПодсказка: версии PostgreSQL сервера и pg_dump должны совпадать по major.\n"
                f"Текущий pg_dump: {cur}\n"
                "Нужно установить pg_dump 16.x и указать полный путь в BACKUP_PG_DUMP_BIN."
            )
        full = f"pg_dump завершился с кодом {proc.returncode}\n{err}{extra}"
        if notify:
            await notify_admin_plain(
                settings,
                text=f"💾 Бэкап: {full}",
                topic=AdminLogTopic.BACKUPS,
                event_type="backup_error",
            )
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False, full

    try:
        with sql_p.open("rb") as f_in:
            with gzip.open(gz_path, "wb", compresslevel=9) as f_out:
                f_out.writelines(f_in)
    finally:
        sql_p.unlink(missing_ok=True)

    try:
        with tarfile.open(dir_archive, "w:gz") as tar:
            tar.add(project_dir, arcname=project_name)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        msg = f"Не удалось упаковать папку проекта {project_dir}: {exc}"
        if notify:
            await notify_admin_plain(
                settings,
                text=f"💾 Бэкап: {msg}",
                topic=AdminLogTopic.BACKUPS,
                event_type="backup_error",
            )
        return False, msg

    with tarfile.open(final_archive, "w:gz") as tar:
        tar.add(gz_path, arcname=gz_path.name)
        tar.add(dir_archive, arcname=dir_archive.name)

    gz_path.unlink(missing_ok=True)
    dir_archive.unlink(missing_ok=True)

    size = final_archive.stat().st_size
    max_bytes = int(settings.backup_max_telegram_mb * 1024 * 1024)
    mb = size / (1024 * 1024)
    fname = final_archive.name

    if size > max_bytes:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        msg = (
            f"Backup создан, но файл слишком большой для Telegram "
            f"({mb:.1f} МБ > {settings.backup_max_telegram_mb:.0f} МБ). "
            "Настройте внешнее хранилище или cron с pg_dump на сервере."
        )
        if notify:
            await notify_admin_plain(
                settings,
                text=f"💾 {msg}",
                topic=AdminLogTopic.BACKUPS,
                event_type="backup_too_large",
            )
        return False, msg

    ok = await notify_admin_document(
        settings,
        document_path=str(final_archive),
        caption=f"💾 Backup  | dir:{project_name} | sql:{dbname} · {mb:.2f} МБ · {stamp_display}",
        topic=AdminLogTopic.BACKUPS,
        event_type="backup",
    )
    shutil.rmtree(tmp_dir, ignore_errors=True)
    if not ok:
        logger.warning("backup: отправка файла в Telegram не удалась")
        return False, "Файл бэкапа создан, но отправка в Telegram не удалась."
    return True, f"Бэкап отправлен: {fname} ({mb:.2f} МБ)."


async def run_daily_backup(settings: Settings) -> None:
    await run_backup_once(settings, notify=True)
