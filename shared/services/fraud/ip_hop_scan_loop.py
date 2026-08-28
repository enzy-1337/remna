"""Фоновый опрос Remnawave connections API по нодам — детектор смены IP.
Интервал: Settings.fraud_ip_hop_poll_interval_sec (по умолчанию 20с — теснее, чем у остальных
циклов, т.к. смысл детектора именно в коротких окнах в десятки секунд).

Ноды не шардируются (в отличие от device_daily_midnight_loop/traffic_spike_scan_loop) —
их обычно на порядки меньше, чем пользователей, а таймингу детектора важна свежесть данных
по каждой ноде на каждом тике.
"""

from __future__ import annotations

import asyncio
import logging
import time

from shared.config import Settings
from shared.database import get_session_factory
from shared.integrations.remnawave import RemnaWaveClient
from shared.services.fraud.ip_hop_detector import (
    build_panel_id_to_user_map,
    check_ip_hop_rules,
    cleanup_old_ip_samples,
    ingest_node_result,
    poll_node_connections,
)

logger = logging.getLogger(__name__)

_ID_MAP_TTL_SEC = 300
_CLEANUP_EVERY_N_TICKS = 120

_id_map_cache: dict[int, int] = {}
_id_map_cached_at = 0.0
_tick_counter = 0


async def _get_id_map(session, rw: RemnaWaveClient) -> dict[int, int]:
    global _id_map_cache, _id_map_cached_at
    now = time.monotonic()
    if _id_map_cache and (now - _id_map_cached_at) < _ID_MAP_TTL_SEC:
        return _id_map_cache
    mapping = await build_panel_id_to_user_map(session, rw)
    if mapping:
        _id_map_cache = mapping
        _id_map_cached_at = now
    return _id_map_cache


async def ip_hop_scan_loop(settings: Settings, stop_event: asyncio.Event) -> None:
    global _tick_counter
    interval = max(15, int(settings.fraud_ip_hop_poll_interval_sec))
    while not stop_event.is_set():
        try:
            rw = RemnaWaveClient(settings)
            async with get_session_factory()() as session:
                async with session.begin():
                    id_map = await _get_id_map(session, rw)
                    if not id_map:
                        logger.debug("ip_hop_scan: panel id -> user map empty, skipping tick")
                    else:
                        node_uuids = await rw.list_node_uuids()
                        touched: set[int] = set()
                        for node_uuid in node_uuids:
                            result = await poll_node_connections(rw, node_uuid)
                            if result is None:
                                continue
                            touched |= await ingest_node_result(
                                session, node_uuid=node_uuid, result=result, panel_id_to_user_id=id_map
                            )
                        if touched:
                            await check_ip_hop_rules(session, settings, touched_user_ids=touched)

                    _tick_counter += 1
                    if _tick_counter % _CLEANUP_EVERY_N_TICKS == 0:
                        removed = await cleanup_old_ip_samples(session)
                        if removed:
                            logger.info("ip_hop_scan: cleaned %s old samples", removed)
        except Exception:
            logger.exception("ip_hop_scan_loop failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
