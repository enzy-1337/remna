"""Детектор смены IP: опрашивает Remnawave ip-control API (панель 2.x; в 3.0.0+ модуль
переименован в `connections` с тем же паттерном job/result, но другим путём — см.
remnawave.py:request_users_ips_by_node) по нодам, копит IpConnectionSample и реагирует
на всплеск различных IP у одного пользователя за короткое окно.

Асинхронный job-паттерн панели (POST запускает задачу, GET по jobId её опрашивает) и то,
что ip-control API адресует пользователей ЧИСЛОВЫМ id панели (переданным строкой в JSON —
подтверждено Zod-схемой `userId: z.string()` в исходниках remnawave/backend тега 2.8.1),
а не uuid (в отличие от остальной части этого клиента, работающей через uuid) —
подтверждено официальным исходным кодом. Сопоставление panel numeric id -> наш User.id
строится ЭМПИРИЧЕСКИ из фактического ответа list_all_users() (см. build_panel_id_to_user_map),
а не жёстко зашито — если числового id там нет, сопоставление получится пустым и детектор
тихо ничего не найдёт (см. лог), вместо падения или ложных срабатываний.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.fraud_incident import FraudIncident
from shared.models.ip_connection_sample import IpConnectionSample
from shared.models.user import User
from shared.services.fraud.confidence import ip_hop_confidence
from shared.services.fraud.incident_service import record_incident

logger = logging.getLogger(__name__)

_RETENTION = timedelta(days=7)
_JOB_POLL_ATTEMPTS = 6
_JOB_POLL_DELAY_SEC = 0.5


async def build_panel_id_to_user_map(session: AsyncSession, rw: RemnaWaveClient) -> dict[int, int]:
    """{panel numeric user id -> наш User.id}, построено из фактического ответа панели.
    Пустой словарь (а не исключение), если панель не отдаёт числовой id в users-ответе."""
    try:
        panel_users = await rw.list_all_users()
    except RemnaWaveError as e:
        logger.debug("ip_hop: list_all_users failed: %s", e)
        return {}

    telegram_by_panel_id: dict[int, int] = {}
    for u in panel_users:
        raw_id = u.get("id")
        tg_id = u.get("telegramId")
        if raw_id is None or tg_id is None:
            continue
        try:
            telegram_by_panel_id[int(raw_id)] = int(tg_id)
        except (TypeError, ValueError):
            continue
    if not telegram_by_panel_id:
        return {}

    tg_ids = list(set(telegram_by_panel_id.values()))
    rows = (
        await session.execute(select(User.id, User.telegram_id).where(User.telegram_id.in_(tg_ids)))
    ).all()
    user_by_telegram = {tg: uid for uid, tg in rows}

    return {
        panel_id: user_by_telegram[tg_id]
        for panel_id, tg_id in telegram_by_panel_id.items()
        if tg_id in user_by_telegram
    }


async def poll_node_connections(rw: RemnaWaveClient, node_uuid: str) -> dict | None:
    """Запускает job и ждёт его завершения (до _JOB_POLL_ATTEMPTS попыток) — возвращает
    result.users (см. get_users_ips_by_node_result) или None при ошибке/таймауте/незавершении."""
    try:
        job_id = await rw.request_users_ips_by_node(node_uuid)
    except RemnaWaveError as e:
        logger.debug("ip_hop: request_users_ips_by_node node=%s failed: %s", node_uuid, e)
        return None
    if not job_id:
        return None

    for _ in range(_JOB_POLL_ATTEMPTS):
        try:
            data = await rw.get_users_ips_by_node_result(job_id)
        except RemnaWaveError as e:
            logger.debug("ip_hop: get_users_ips_by_node_result node=%s failed: %s", node_uuid, e)
            return None
        if data.get("isFailed"):
            return None
        if data.get("isCompleted"):
            result = data.get("result")
            return result if isinstance(result, dict) else None
        await asyncio.sleep(_JOB_POLL_DELAY_SEC)
    return None


def _parse_last_seen(raw: object) -> datetime:
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def ingest_node_result(
    session: AsyncSession,
    *,
    node_uuid: str,
    result: dict,
    panel_id_to_user_id: dict[int, int],
) -> set[int]:
    """Пишет IpConnectionSample по каждому известному нам пользователю в результате ноды.
    Возвращает множество затронутых User.id (для последующей проверки правил)."""
    touched: set[int] = set()
    users = result.get("users")
    if not isinstance(users, list):
        return touched
    for entry in users:
        if not isinstance(entry, dict):
            continue
        raw_panel_id = entry.get("userId")
        try:
            # ip-control отдаёт userId строкой (Zod: z.string()), а не числом — приводим явно,
            # иначе сопоставление всегда будет пустым и детектор молча ничего не найдёт.
            panel_id = int(raw_panel_id) if raw_panel_id is not None else None
        except (TypeError, ValueError):
            panel_id = None
        user_id = panel_id_to_user_id.get(panel_id) if panel_id is not None else None
        if user_id is None:
            continue
        ips = entry.get("ips")
        if not isinstance(ips, list):
            continue
        for ip_entry in ips:
            if not isinstance(ip_entry, dict):
                continue
            ip = str(ip_entry.get("ip") or "").strip()
            if not ip:
                continue
            session.add(
                IpConnectionSample(
                    user_id=user_id,
                    ip=ip,
                    node_uuid=node_uuid,
                    seen_at=_parse_last_seen(ip_entry.get("lastSeen")),
                )
            )
            touched.add(user_id)
    return touched


async def check_ip_hop_rules(session: AsyncSession, settings: Settings, *, touched_user_ids: set[int]) -> None:
    """Для каждого затронутого в этом тике пользователя — жёсткое правило (мгновенный автобан,
    вне staged rollout) и мягкое правило (через record_incident/staged rollout)."""
    if not touched_user_ids:
        return
    now = datetime.now(timezone.utc)

    hard_window = now - timedelta(seconds=settings.fraud_ip_hop_hard_block_window_sec)
    hard_rows = (
        await session.execute(
            select(IpConnectionSample.user_id, func.count(func.distinct(IpConnectionSample.ip)))
            .where(IpConnectionSample.user_id.in_(touched_user_ids), IpConnectionSample.seen_at >= hard_window)
            .group_by(IpConnectionSample.user_id)
        )
    ).all()
    hard_hits = {uid: cnt for uid, cnt in hard_rows if cnt >= settings.fraud_ip_hop_hard_block_ip_count}

    soft_window = now - timedelta(seconds=settings.fraud_ip_hop_suspicious_window_sec)
    soft_rows = (
        await session.execute(
            select(IpConnectionSample.user_id, func.count(func.distinct(IpConnectionSample.ip)))
            .where(IpConnectionSample.user_id.in_(touched_user_ids), IpConnectionSample.seen_at >= soft_window)
            .group_by(IpConnectionSample.user_id)
        )
    ).all()
    soft_hits = {uid: cnt for uid, cnt in soft_rows if cnt >= settings.fraud_ip_hop_suspicious_ip_count}

    # Скользящее окно пересчитывается каждый тик — без этой защиты один и тот же всплеск
    # мог бы породить несколько инцидентов подряд, пока не «выпадет» из окна.
    recent_cooldown = now - timedelta(
        seconds=max(settings.fraud_ip_hop_hard_block_window_sec, settings.fraud_ip_hop_suspicious_window_sec)
    )
    recently_incidented = set(
        (
            await session.execute(
                select(FraudIncident.user_id).where(
                    FraudIncident.user_id.in_(touched_user_ids),
                    FraudIncident.detector == "ip_hop",
                    FraudIncident.created_at >= recent_cooldown,
                )
            )
        ).scalars()
    )

    for user_id in touched_user_ids:
        if user_id in recently_incidented:
            continue
        user = await session.get(User, user_id)
        if user is None:
            continue

        if user_id in hard_hits:
            cnt = hard_hits[user_id]
            await record_incident(
                session,
                settings,
                user=user,
                detector="ip_hop",
                severity="hard_violation",
                confidence=1.0,
                reason_text=(
                    f"{cnt} разных IP за {settings.fraud_ip_hop_hard_block_window_sec} сек — "
                    "явный признак шеринга/компрометации подписки."
                ),
                evidence={"distinct_ip_count": cnt, "window_sec": settings.fraud_ip_hop_hard_block_window_sec},
                force_auto_block=True,
            )
            continue  # жёсткое правило перекрывает мягкое для этого пользователя в этом тике

        if user_id in soft_hits:
            cnt = soft_hits[user_id]
            confidence, breakdown = ip_hop_confidence(
                distinct_ip_count=cnt, suspicious_threshold=settings.fraud_ip_hop_suspicious_ip_count
            )
            await record_incident(
                session,
                settings,
                user=user,
                detector="ip_hop",
                severity="suspicious",
                confidence=confidence,
                reason_text=(
                    f"{cnt} разных IP за {settings.fraud_ip_hop_suspicious_window_sec} сек — "
                    "подозрительно частая смена адреса."
                ),
                evidence={
                    "distinct_ip_count": cnt,
                    "window_sec": settings.fraud_ip_hop_suspicious_window_sec,
                    "confidence_breakdown": breakdown,
                },
            )


async def cleanup_old_ip_samples(session: AsyncSession) -> int:
    cutoff = datetime.now(timezone.utc) - _RETENTION
    res = await session.execute(delete(IpConnectionSample).where(IpConnectionSample.seen_at < cutoff))
    return int(res.rowcount or 0)
