"""HTTP-клиент Remnawave Panel API (OpenAPI /api/users)."""

from __future__ import annotations

import asyncio
import logging
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)


class RemnaWaveError(Exception):
    """Ошибка вызова Remnawave API."""


class RemnaWaveClient:
    """Минимальный клиент для шага 6 (создание пользователя, ссылка подписки)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._origin = self._s.remnawave_api_url.rstrip("/")
        raw = (self._s.remnawave_api_path_prefix or "").strip()
        if raw in ("", "/"):
            self._api_prefix = ""
        else:
            self._api_prefix = raw if raw.startswith("/") else f"/{raw}"
            self._api_prefix = self._api_prefix.rstrip("/")

    def _url(self, resource: str) -> str:
        """resource: 'users' или 'users/{uuid}'."""
        res = resource.lstrip("/")
        if self._api_prefix:
            return f"{self._origin}{self._api_prefix}/{res}"
        return f"{self._origin}/{res}"

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self._s.remnawave_api_token.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        cookie = (self._s.remnawave_cookie or "").strip()
        if cookie:
            # Документация может давать cookie как "NAME=VALUE" (как в nginx map).
            # Поэтому поддерживаем оба формата:
            # 1) "aEmFnBcC=WbYWpixX" (оставляем как есть)
            # 2) "WbYWpixX" (тогда используем имя "__remnawave-reverse-proxy__")
            if "=" in cookie:
                h["Cookie"] = cookie
            else:
                h["Cookie"] = f"__remnawave-reverse-proxy__={cookie}"
        return h

    def _unwrap(self, data: dict[str, Any]) -> dict[str, Any]:
        if "response" in data and isinstance(data["response"], dict):
            return data["response"]
        return data

    async def _request(
        self,
        method: str,
        resource: str,
        *,
        json_body: dict | None = None,
    ) -> dict[str, Any]:
        if self._s.remnawave_stub:
            raise RuntimeError("Используйте create_user_stub / get_user_stub при REMNAWAVE_STUB")

        url = self._url(resource)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self._s.remnawave_request_timeout) as client:
                    r = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        json=json_body,
                    )
                if r.status_code == 401:
                    logger.error("Remnawave 401 Unauthorized — проверьте REMNAWAVE_API_TOKEN")
                    raise RemnaWaveError("Неверный или просроченный токен Remnawave")
                if r.status_code >= 400:
                    logger.warning(
                        "Remnawave %s %s -> %s %s",
                        method,
                        resource,
                        r.status_code,
                        r.text[:500],
                    )
                    raise RemnaWaveError(f"Remnawave HTTP {r.status_code}: {r.text[:200]}")
                return r.json()
            except httpx.RequestError as e:
                last_exc = e
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        raise RemnaWaveError(str(last_exc)) from last_exc

    async def create_user(
        self,
        *,
        username: str,
        expire_at: datetime,
        traffic_limit_bytes: int,
        description: str,
        telegram_id: int,
        hwid_device_limit: int,
        active_internal_squads: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /api/users — возвращает объект пользователя (unwrap response)."""
        if self._s.remnawave_stub:
            uid = str(uuid_lib.uuid4())
            return {
                "uuid": uid,
                "username": username,
                "subscriptionUrl": f"https://stub.remnawave.local/sub/{uid}",
            }

        expire_iso = expire_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body: dict[str, Any] = {
            "username": username,
            "expireAt": expire_iso,
            "trafficLimitBytes": traffic_limit_bytes,
            "description": description,
            "telegramId": telegram_id,
            "hwidDeviceLimit": hwid_device_limit,
            "trafficLimitStrategy": "NO_RESET",
        }
        if active_internal_squads:
            body["activeInternalSquads"] = active_internal_squads

        data = await self._request("POST", "users", json_body=body)
        return self._unwrap(data)

    async def get_user(self, user_uuid: str) -> dict[str, Any]:
        """GET /api/users/{uuid}"""
        if self._s.remnawave_stub:
            return {
                "uuid": user_uuid,
                "subscriptionUrl": f"https://stub.remnawave.local/sub/{user_uuid}",
                "expireAt": (datetime.now(timezone.utc) + timedelta(days=30)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                ),
                "hwidDeviceLimit": 2,
                "trafficLimitBytes": 1024**3,
                "usedTrafficBytes": 0,
                "status": "ACTIVE",
            }
        data = await self._request("GET", f"users/{user_uuid}")
        return self._unwrap(data)

    async def update_user(
        self,
        user_uuid: str,
        *,
        expire_at: datetime | None = None,
        hwid_device_limit: int | None = None,
        traffic_limit_bytes: int | None = None,
        status: str | None = None,
        description: str | None = None,
        active_internal_squads: list[str] | None = None,
    ) -> dict[str, Any]:
        """PATCH /api/users/{uuid} — частичное обновление."""
        body: dict[str, Any] = {}
        if expire_at is not None:
            body["expireAt"] = expire_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if hwid_device_limit is not None:
            body["hwidDeviceLimit"] = hwid_device_limit
        if traffic_limit_bytes is not None:
            body["trafficLimitBytes"] = traffic_limit_bytes
        if status is not None:
            body["status"] = status
        if description is not None:
            body["description"] = description
        if active_internal_squads is not None:
            body["activeInternalSquads"] = active_internal_squads

        if self._s.remnawave_stub:
            cur = await self.get_user(user_uuid)
            if expire_at is not None:
                cur["expireAt"] = body["expireAt"]
            if hwid_device_limit is not None:
                cur["hwidDeviceLimit"] = hwid_device_limit
            if traffic_limit_bytes is not None:
                cur["trafficLimitBytes"] = traffic_limit_bytes
            if status is not None:
                cur["status"] = status
            return cur

        if not body:
            return await self.get_user(user_uuid)
        data = await self._request("PATCH", f"users/{user_uuid}", json_body=body)
        return self._unwrap(data)

    @staticmethod
    def default_expire(days: int) -> datetime:
        return datetime.now(timezone.utc) + timedelta(days=days)
