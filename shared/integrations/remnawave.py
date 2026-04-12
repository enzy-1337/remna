"""HTTP-клиент Remnawave Panel API.

Спецификация: https://docs.rw/api (Scalar) · OpenAPI JSON: https://cdn.docs.rw/docs/openapi.json

Актуальные пути (Users): POST/PATCH ``/api/users``, GET ``/api/users/{uuid}``, GET ``/api/users`` с ``start``/``size``,
GET ``/api/users/by-telegram-id/{telegramId}``, POST ``/api/users/bulk/update``. Обновление по UUID — через PATCH на
коллекцию ``users`` с телом ``{ "uuid": "...", ... }``, а не PATCH ``/users/{uuid}``.
Перевыпуск ссылки подписки (новый short UUID и ``subscriptionUrl``): POST ``/api/users/{uuid}/actions/revoke``
(``UsersController_revokeUserSubscription``), тело ``{ "revokeOnlyPasswords": false }`` (или пустой объект — по умолчанию
меняется UUID ссылки; при ``true`` сбрасываются только пароли без смены short UUID).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)


class RemnaWaveError(Exception):
    """Ошибка вызова Remnawave API."""


def subscription_url_for_telegram(raw: str | None, settings: Settings) -> str | None:
    """Ссылка подписки для пользователя в Telegram.

    Если задан ``REMNAWAVE_PUBLIC_URL``, подменяет схему и хост из ответа панели,
    путь и query сохраняются. Нужно, когда ``REMNAWAVE_API_URL`` указывает на
    localhost или внутренний адрес, а клиентам нужен внешний домен панели.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    pub_raw = (settings.remnawave_public_url or "").strip()
    if not pub_raw:
        return s
    pub_s = pub_raw if "://" in pub_raw else f"https://{pub_raw}"
    try:
        parsed = urlparse(s)
        pub = urlparse(pub_s)
        if not pub.scheme or not pub.netloc:
            return s
        if not parsed.netloc:
            base = pub.geturl().rstrip("/")
            path = parsed.path or ""
            if path and not path.startswith("/"):
                path = "/" + path
            q = f"?{parsed.query}" if parsed.query else ""
            frag = f"#{parsed.fragment}" if parsed.fragment else ""
            return f"{base}{path}{q}{frag}" if path or q or frag else base
        new = parsed._replace(scheme=pub.scheme, netloc=pub.netloc)
        return urlunparse(new)
    except Exception:
        return s


def is_remnawave_not_found(err: BaseException) -> bool:
    """Панель вернула 404 (учётка удалена или uuid не существует)."""
    if not isinstance(err, RemnaWaveError):
        return False
    return "HTTP 404" in str(err)


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

    def _extract_users_list(self, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if not isinstance(data, dict):
            return []
        # Напр. GET .../by-telegram-id/{id} → { "response": [ {user}, ... ] }
        top = data.get("response")
        if isinstance(top, list):
            return [x for x in top if isinstance(x, dict)]
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
                    # GET users/{uuid} 404 — обычно удалённый пользователь; не засоряем WARNING при каждом sync
                    res_parts = resource.split("/")
                    quiet_404_user = (
                        method == "GET"
                        and r.status_code == 404
                        and len(res_parts) == 2
                        and res_parts[0] == "users"
                        and not res_parts[1].startswith("by-")
                    )
                    if quiet_404_user:
                        logger.debug(
                            "Remnawave %s %s -> 404 (user not found in panel)",
                            method,
                            resource,
                        )
                    else:
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

    async def reset_user_subscription_credentials(
        self,
        user_uuid: str,
        *,
        revoke_only_passwords: bool = False,
    ) -> dict[str, Any]:
        """
        Перевыпуск подписки одной операцией панели: POST ``/api/users/{uuid}/actions/revoke``.

        При ``revoke_only_passwords=False`` (по умолчанию) панель выдаёт новый short UUID и новую ``subscriptionUrl``.
        При ``True`` — только сброс паролей узлов, URL подписки не меняется.
        """
        body: dict[str, Any] = {"revokeOnlyPasswords": bool(revoke_only_passwords)}
        if self._s.remnawave_stub:
            new_token = str(uuid_lib.uuid4())
            return {
                "uuid": user_uuid,
                "subscriptionUrl": f"https://stub.remnawave.local/sub/{new_token}",
                "shortUuid": new_token.replace("-", "")[:16],
            }
        data = await self._request("POST", f"users/{user_uuid}/actions/revoke", json_body=body)
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
                "trafficLimitStrategy": "NO_RESET",
                "userTraffic": {
                    "usedTrafficBytes": 256 * 1024 * 1024,
                    "lifetimeUsedTrafficBytes": 512 * 1024 * 1024,
                    "onlineAt": None,
                    "firstConnectedAt": None,
                    "lastConnectedNodeUuid": None,
                },
                "status": "ACTIVE",
            }
        data = await self._request("GET", f"users/{user_uuid}")
        raw = self._unwrap(data)
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    return item
            return {}
        logger.warning(
            "Remnawave get_user %s: неожиданный тип ответа %s",
            user_uuid,
            type(raw).__name__,
        )
        return {}

    async def delete_panel_user(self, user_uuid: str) -> None:
        """Удаление пользователя в панели: DELETE /api/users/{uuid} (см. OpenAPI DeleteUser)."""
        if self._s.remnawave_stub:
            return

        def _miss(err: RemnaWaveError) -> bool:
            m = str(err)
            return "HTTP 404" in m or "HTTP 405" in m

        attempts: list[tuple[str, str, dict[str, Any] | None]] = [
            ("DELETE", f"users/{user_uuid}", None),
            ("POST", "users/delete", {"uuid": user_uuid}),
            ("POST", "users/delete", {"userUuid": user_uuid}),
        ]
        last_err: RemnaWaveError | None = None
        for method, resource, body in attempts:
            try:
                await self._request(method, resource, json_body=body)
                return
            except RemnaWaveError as e:
                last_err = e
                if _miss(e):
                    continue
                raise
        if last_err is not None:
            raise last_err

    async def ping_api(self) -> tuple[bool, str, float | None]:
        """Проверка доступности API: GET users с минимальной выборкой."""
        if self._s.remnawave_stub:
            return True, "REMNAWAVE_STUB (запросы к панели отключены)", None
        t0 = time.perf_counter()
        try:
            await self._request("GET", "users", params={"start": 0, "size": 1})
            ms = round((time.perf_counter() - t0) * 1000, 1)
            return True, f"HTTP OK · {ms} мс", ms
        except RemnaWaveError as e:
            return False, str(e)[:240], None

    async def probe_nodes_api(self) -> tuple[bool, str]:
        """Пробуем типичные пути списка нод (зависит от версии Remnawave)."""
        if self._s.remnawave_stub:
            return True, "Заглушка (без сети)"
        for path in ("nodes", "internal-nodes", "system/health"):
            try:
                await self._request("GET", path)
                return True, f"ответ по «{path}»"
            except RemnaWaveError:
                continue
        return False, "Нет подходящего GET-эндпоинта — статус нод смотрите в UI панели"

    def _extract_nodes_list(self, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if not isinstance(data, dict):
            return []
        top = data.get("response")
        if isinstance(top, list):
            return [x for x in top if isinstance(x, dict)]
        root = self._unwrap(data)
        if isinstance(root, list):
            return [x for x in root if isinstance(x, dict)]
        if isinstance(root, dict):
            for key in ("nodes", "internalNodes", "items", "data", "rows", "result"):
                val = root.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
        return []

    async def list_nodes_with_latency(
        self, *, max_nodes: int = 50, ping_each: bool = True
    ) -> tuple[list[dict[str, Any]], float | None, str | None]:
        """Список нод из API; при ping_each=True — дополнительный GET по каждой ноде (мс)."""
        if self._s.remnawave_stub:
            return [
                {
                    "name": "stub-node",
                    "uuid": "00000000-0000-0000-0000-000000000001",
                    "status": "STUB",
                    "ping_ms": None,
                    "ping_note": "REMNAWAVE_STUB",
                }
            ], None, None

        sem = asyncio.Semaphore(6)

        async def _ping_uuid(nu: str) -> tuple[float | None, str]:
            async with sem:
                t0 = time.perf_counter()
                for sub in (f"nodes/{nu}", f"internal-nodes/{nu}"):
                    try:
                        await self._request("GET", sub)
                        ms = round((time.perf_counter() - t0) * 1000, 1)
                        return ms, sub
                    except RemnaWaveError:
                        continue
                return None, ""

        last_list_err: str | None = None
        for path in ("nodes", "internal-nodes"):
            t0 = time.perf_counter()
            try:
                data = await self._request("GET", path)
                catalog_ms = round((time.perf_counter() - t0) * 1000, 1)
                nodes = self._extract_nodes_list(data)
                if not nodes:
                    last_list_err = f"«{path}»: пустой список"
                    continue
                rows_out: list[dict[str, Any]] = []
                for n in nodes[:max_nodes]:
                    nu = str(n.get("uuid") or n.get("id") or n.get("nodeUuid") or "").strip()
                    name = str(n.get("name") or n.get("tag") or n.get("address") or nu or "—")[:120]
                    st = str(n.get("status") or n.get("state") or "—")[:80]
                    if nu:
                        if ping_each:
                            pms, subp = await _ping_uuid(nu)
                            note = f"{subp} · {pms} мс" if pms is not None else "детальный GET недоступен"
                        else:
                            pms, note = None, "без отдельного запроса"
                    else:
                        pms, note = None, "нет uuid в ответе"
                    rows_out.append(
                        {
                            "name": name,
                            "uuid": nu or "—",
                            "status": st,
                            "ping_ms": pms,
                            "ping_note": note[:160],
                        }
                    )
                return rows_out, catalog_ms, None
            except RemnaWaveError as e:
                last_list_err = str(e)[:220]
                continue
        return [], None, last_list_err or "не удалось получить список нод"

    async def list_users(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if self._s.remnawave_stub:
            return []
        # OpenAPI: GET /api/users — query start, size (UsersController_getAllUsers)
        queries: list[dict[str, Any] | None] = [
            {"start": 0, "size": limit},
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
                {"start": (page - 1) * page_size, "size": take},
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
        # OpenAPI: GET /api/users/by-telegram-id/{telegramId}
        try:
            data = await self._request("GET", f"users/by-telegram-id/{telegram_id}")
            items = self._extract_users_list(data)
            for it in items:
                tid = self._coerce_int(it.get("telegramId") or it.get("telegram_id") or it.get("tgId"))
                if tid == telegram_id:
                    return it
            if items:
                return items[0]
        except RemnaWaveError:
            pass
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
        legacy_marker = f"tg_id:{telegram_id}"
        id_in_desc = re.compile(rf"Telegram ID:\s*{telegram_id}\b")
        for it in users:
            tid = self._coerce_int(it.get("telegramId") or it.get("telegram_id") or it.get("tgId"))
            if tid == telegram_id:
                return it
            desc = str(it.get("description") or "")
            tag = str(it.get("tag") or "")
            if legacy_marker in desc or legacy_marker in tag:
                return it
            if id_in_desc.search(desc) or id_in_desc.search(tag):
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

        # OpenAPI: PATCH /api/users + UpdateUserRequestDto (uuid в теле); bulk — POST /api/users/bulk/update
        single_body: dict[str, Any] = {"uuid": user_uuid, **body}
        bulk_body: dict[str, Any] = {"uuids": [user_uuid], "fields": body}
        attempts: list[tuple[str, str, dict[str, Any]]] = [
            ("PATCH", "users", single_body),
            ("POST", "users/bulk/update", bulk_body),
            ("PUT", "users", single_body),
            ("PATCH", f"users/{user_uuid}", body),
            ("PUT", f"users/{user_uuid}", body),
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

    async def get_user_hwid_devices(self, user_uuid: str) -> list[dict[str, Any]]:
        """GET /api/hwid/devices/{userUuid} → response.devices (OpenAPI)."""
        if self._s.remnawave_stub:
            return [
                {
                    "hwid": "stub_hwid_1",
                    "userUuid": user_uuid,
                    "platform": "Android",
                    "osVersion": "14",
                    "deviceModel": "Pixel 8",
                    "userAgent": "koala-clash/1.1.0",
                    "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                }
            ]
        data = await self._request("GET", f"hwid/devices/{user_uuid}")
        root = data.get("response") if isinstance(data, dict) else None
        if not isinstance(root, dict):
            root = self._unwrap(data) if isinstance(data, dict) else {}
        devs = root.get("devices") if isinstance(root, dict) else None
        if not isinstance(devs, list):
            return []
        return [x for x in devs if isinstance(x, dict)]

    async def delete_user_hwid_device(self, user_uuid: str, hwid: str) -> None:
        """POST /api/hwid/devices/delete — тело { userUuid, hwid }."""
        if self._s.remnawave_stub:
            return
        await self._request(
            "POST",
            "hwid/devices/delete",
            json_body={"userUuid": user_uuid, "hwid": hwid},
        )

    @staticmethod
    def default_expire(days: int) -> datetime:
        return datetime.now(timezone.utc) + timedelta(days=days)
