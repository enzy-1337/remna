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

    def _extract_users_list(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        root = self._unwrap(data)
        if isinstance(root, list):
            return [x for x in root if isinstance(x, dict)]
        if isinstance(root, dict):
            for key in ("items", "users", "data", "rows", "result"):
                val = root.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
        return []

    async def _request(
        self,
        method: str,
        resource: str,
        *,
        json_body: dict | None = None,
        params: dict[str, Any] | None = None,
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
                        params=params,
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

    async def list_users(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if self._s.remnawave_stub:
            return []
        queries: list[dict[str, Any] | None] = [
            {"limit": limit},
            {"page": 1, "limit": limit},
            {"take": limit},
            None,
        ]
        for q in queries:
            try:
                data = await self._request("GET", "users", params=q)
                items = self._extract_users_list(data)
                if items:
                    return items
            except RemnaWaveError:
                continue
        return []

    async def list_all_users(
        self,
        *,
        page_size: int = 200,
        max_items: int = 5000,
        max_pages: int = 500,
    ) -> list[dict[str, Any]]:
        """Все пользователи панели с пагинацией (page/limit и skip/take)."""
        if self._s.remnawave_stub:
            return []
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            if len(out) >= max_items:
                break
            take = min(page_size, max_items - len(out))
            if take <= 0:
                break
            batch: list[dict[str, Any]] = []
            param_sets = [
                {"page": page, "limit": take},
                {"skip": (page - 1) * page_size, "take": take},
                {"offset": (page - 1) * page_size, "limit": take},
            ]
            for q in param_sets:
                try:
                    data = await self._request("GET", "users", params=q)
                    items = self._extract_users_list(data)
                    if items:
                        batch = items
                        break
                except RemnaWaveError:
                    continue
            if not batch:
                break
            new_in_batch = 0
            for it in batch:
                uid = str(it.get("uuid") or "").strip()
                if uid and uid not in seen:
                    seen.add(uid)
                    out.append(it)
                    new_in_batch += 1
                    if len(out) >= max_items:
                        break
            if new_in_batch == 0:
                break
            if len(batch) < take:
                break
        return out

    @staticmethod
    def _coerce_int(val: Any) -> int | None:
        try:
            return int(str(val).strip())
        except (TypeError, ValueError):
            return None

    async def find_user_by_telegram_id(self, telegram_id: int) -> dict[str, Any] | None:
        if self._s.remnawave_stub:
            return None
        filters = [
            {"telegramId": telegram_id},
            {"telegram_id": telegram_id},
            {"search": str(telegram_id)},
            {"q": str(telegram_id)},
        ]
        for params in filters:
            try:
                data = await self._request("GET", "users", params=params)
                for it in self._extract_users_list(data):
                    tid = self._coerce_int(it.get("telegramId") or it.get("telegram_id") or it.get("tgId"))
                    if tid == telegram_id:
                        return it
            except RemnaWaveError:
                continue
        users = await self.list_users(limit=500)
        marker = f"tg_id:{telegram_id}"
        for it in users:
            tid = self._coerce_int(it.get("telegramId") or it.get("telegram_id") or it.get("tgId"))
            if tid == telegram_id:
                return it
            desc = str(it.get("description") or "")
            tag = str(it.get("tag") or "")
            if marker in desc or marker in tag:
                return it
        return None

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
        """Обновление пользователя (разные версии API панели)."""
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
        def _is_path_or_method_miss(err: RemnaWaveError) -> bool:
            msg = str(err)
            return ("HTTP 404" in msg) or ("HTTP 405" in msg)

        # Встречаются разные варианты API:
        # - PATCH/PUT /users/{uuid}
        # - PATCH/PUT /users  (single update body)
        # - */users/bulk-update с {uuids, fields}
        single_body: dict[str, Any] = {"uuid": user_uuid, **body}
        bulk_body: dict[str, Any] = {"uuids": [user_uuid], "fields": body}
        attempts: list[tuple[str, str, dict[str, Any]]] = [
            ("PATCH", f"users/{user_uuid}", body),
            ("PUT", f"users/{user_uuid}", body),
            ("PATCH", "users", single_body),
            ("PUT", "users", single_body),
            ("PATCH", "users/bulk-update", bulk_body),
            ("PUT", "users/bulk-update", bulk_body),
            ("POST", "users/bulk-update", bulk_body),
            ("PATCH", "users", bulk_body),
            ("POST", "users", bulk_body),
        ]
        last_err: RemnaWaveError | None = None
        for method, resource, payload in attempts:
            try:
                data = await self._request(method, resource, json_body=payload)
                return self._unwrap(data)
            except RemnaWaveError as e:
                last_err = e
                if _is_path_or_method_miss(e):
                    continue
                raise
        raise last_err or RemnaWaveError("Не удалось обновить пользователя Remnawave")

    @staticmethod
    def default_expire(days: int) -> datetime:
        return datetime.now(timezone.utc) + timedelta(days=days)
