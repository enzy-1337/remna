from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.database import get_session_factory
from shared.models.device_history import DeviceHistory
from shared.models.remnawave_webhook_event import RemnawaveWebhookEvent
from shared.models.user import User
from shared.services.billing_v2.charging_policy import applies_pay_per_use_charges
from shared.services.billing_v2.device_service import add_device_history_event
from shared.services.billing_v2.billing_calendar import billing_today
from shared.services.billing_v2.rating_service import charge_daily_device_once, charge_gb_step


def telegram_id_from_payload(payload: dict) -> int | None:
    def _coerce(raw: object) -> int | None:
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            val = raw.strip()
            if val.isdigit():
                try:
                    return int(val)
                except ValueError:
                    return None
        return None

    def _from_dict(d: dict) -> int | None:
        for key in ("telegram_id", "telegramId", "tg_id", "tgId"):
            tid = _coerce(d.get(key))
            if tid is not None:
                return tid
        return None

    tid = _from_dict(payload)
    if tid is not None:
        return tid
    data = payload.get("data")
    if isinstance(data, dict):
        tid = _from_dict(data)
        if tid is not None:
            return tid
        user = data.get("user")
        if isinstance(user, dict):
            return _from_dict(user)
    return None


def remnawave_user_uuid_from_payload(payload: dict) -> uuid.UUID | None:
    """UUID пользователя панели (data.user.uuid) — если telegramId в вебхуке null."""

    def _parse(d: dict) -> uuid.UUID | None:
        for key in ("uuid", "userUuid", "user_uuid"):
            raw = d.get(key)
            if raw is None:
                continue
            try:
                return uuid.UUID(str(raw).strip())
            except ValueError:
                continue
        return None

    u = _parse(payload)
    if u is not None:
        return u
    data = payload.get("data")
    if isinstance(data, dict):
        u = _parse(data)
        if u is not None:
            return u
        user = data.get("user")
        if isinstance(user, dict):
            return _parse(user)
    return None


def device_hwid_from_payload(payload: dict) -> str:
    """Кастомные вебхуки и нативные Remnawave (@remnawave/backend-contract: data.hwidUserDevice.hwid)."""

    def _pick(d: object) -> str:
        if not isinstance(d, dict):
            return ""
        for key in ("device_hwid", "hwid", "deviceHwid", "hardwareId"):
            raw = d.get(key)
            if raw is None:
                continue
            s = str(raw).strip()
            if s:
                return s
        return ""

    h = _pick(payload)
    if h:
        return h
    data = payload.get("data")
    if isinstance(data, dict):
        h = _pick(data)
        if h:
            return h
        device = data.get("device")
        h = _pick(device)
        if h:
            return h
        hud = data.get("hwidUserDevice")
        h = _pick(hud)
        if h:
            return h
    return ""


def device_identity_meta_from_payload(payload: dict) -> dict[str, str]:
    """
    Необязательные поля устройства из вебхука Remnawave (аудит / поддержка).
    На списание за день **не влияют** — идемпотентность по HWID и календарной дате биллинга.
    """

    def _str(v: object, max_len: int) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        return s[:max_len] if s else ""

    def _scan(d: object, out: dict[str, str]) -> None:
        if not isinstance(d, dict):
            return
        candidates: tuple[tuple[str, str], ...] = (
            ("device_name", "name"),
            ("device_name", "deviceName"),
            ("device_name", "displayName"),
            ("device_model", "model"),
            ("device_model", "deviceModel"),
            ("rw_device_uuid", "uuid"),
            ("rw_device_uuid", "deviceUuid"),
            ("rw_user_device_id", "id"),
        )
        for out_key, in_key in candidates:
            if out.get(out_key):
                continue
            val = _str(d.get(in_key), 256)
            if val:
                out[out_key] = val

    out: dict[str, str] = {}
    _scan(payload, out)
    data = payload.get("data")
    if isinstance(data, dict):
        _scan(data, out)
        for nest in ("device", "hwidUserDevice", "userDevice"):
            n = data.get(nest)
            if isinstance(n, dict):
                _scan(n, out)
    return out


def _parse_webhook_unix_ts(ts_header: str) -> int | None:
    """Секунды Unix; панель может слать миллисекунды (13 цифр) или float."""
    raw = (ts_header or "").strip()
    if not raw:
        return None
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        try:
            f = float(raw)
            if f > 10_000_000_000:
                f /= 1000.0
            ts = int(f)
        except (TypeError, ValueError):
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except Exception:
                return None
    if ts > 10_000_000_000:
        ts //= 1000
    return ts


def _ts_header_looks_like_unix_epoch(raw: str) -> bool:
    """
    Анти-replay по TTL имеет смысл только для «момента отправки» (Unix).
    Remnawave кладёт в заголовок ISO из payload (см. docs.rw) — это время события,
    оно может сильно отличаться от момента HTTP-запроса; TTL по нему ломает валидную подпись.
    """
    s = (raw or "").strip()
    if not s or "T" in s or s.endswith("Z"):
        return False
    return bool(re.fullmatch(r"\d{9,}(\.\d+)?", s))


def _normalize_signature_hex_header(signature_header: str) -> str:
    """Сравнение без учёта регистра hex (панель может слать A-F)."""
    sig = signature_header.strip()
    low = sig.lower()
    if low.startswith("sha256="):
        return "sha256=" + low[7:]
    return low


def _hmac_sha256_hex_matches(*, secret: bytes, message: bytes, signature_header: str) -> bool:
    digest = hmac.new(secret, message, hashlib.sha256).hexdigest()
    sig = _normalize_signature_hex_header(signature_header)
    if hmac.compare_digest(f"sha256={digest}", sig):
        return True
    return hmac.compare_digest(digest, sig)


def _remnawave_hmac_key_candidates(secret_raw: str) -> list[bytes]:
    """
    Панель может подписывать как UTF-8 строкой из .env, так и байтами из hex
    (аналог Buffer.from(secret, 'hex') в Node) — типично для секрета из 64 hex-символов.
    """
    out: list[bytes] = [secret_raw.encode("utf-8")]
    if (
        len(secret_raw) >= 32
        and len(secret_raw) % 2 == 0
        and re.fullmatch(r"[0-9a-fA-F]+", secret_raw) is not None
    ):
        out.append(bytes.fromhex(secret_raw))
    return out


def verify_remnawave_signature(
    *,
    body: bytes,
    ts_header: str,
    signature_header: str,
    settings: Settings,
) -> bool:
    secret_raw = (settings.remnawave_webhook_secret or "").strip()
    if not secret_raw:
        return False
    sig = (signature_header or "").strip()
    if not sig:
        return False

    raw_ts_header = (ts_header or "").strip()
    has_ts = bool(raw_ts_header)
    ts_norm = _parse_webhook_unix_ts(ts_header) if has_ts else None
    # Непарсящийся timestamp не блокирует HMAC: в docs.rw TS-пример проверяет только подпись.
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if (
        has_ts
        and _ts_header_looks_like_unix_epoch(raw_ts_header)
        and ts_norm is not None
        and abs(now_ts - ts_norm) > settings.remnawave_webhook_signature_ttl_sec
    ):
        return False

    for secret in _remnawave_hmac_key_candidates(secret_raw):
        # 1) Как в Go-примере docs.rw: HMAC от сырого тела.
        if _hmac_sha256_hex_matches(secret=secret, message=body, signature_header=sig):
            return True
        # 2) Канонический JSON (как при повторной сериализации; ensure_ascii как в JS по умолчанию).
        try:
            text = body.decode("utf-8")
            if text.startswith("\ufeff"):
                text = text[1:]
            obj = json.loads(text)
            for sk in (False, True):
                for ascii_flag in (True, False):
                    compact = json.dumps(
                        obj, separators=(",", ":"), sort_keys=sk, ensure_ascii=ascii_flag
                    ).encode("utf-8")
                    if _hmac_sha256_hex_matches(secret=secret, message=compact, signature_header=sig):
                        return True
        except Exception:
            pass
        # 3) Старый вариант: "{unix}." + сырое тело (совместимость).
        if has_ts:
            for prefix in (f"{raw_ts_header}.", f"{ts_norm}." if ts_norm is not None else None):
                if prefix is None:
                    continue
                msg = prefix.encode("utf-8") + body
                if _hmac_sha256_hex_matches(secret=secret, message=msg, signature_header=sig):
                    return True
    return False


async def store_raw_webhook_event(
    session: AsyncSession,
    *,
    event_id: str,
    event_type: str,
    payload: dict,
    headers: dict[str, str],
    signature_valid: bool,
) -> tuple[RemnawaveWebhookEvent, bool]:
    existing = (
        await session.execute(
            select(RemnawaveWebhookEvent).where(RemnawaveWebhookEvent.event_id == event_id).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status != "duplicate":
            existing.status = "duplicate"
            await session.flush()
        return existing, True
    row = RemnawaveWebhookEvent(
        event_id=event_id,
        event_type=event_type,
        payload=payload,
        headers=headers,
        signature_valid=signature_valid,
        status="received",
    )
    session.add(row)
    await session.flush()
    return row, False


async def process_remnawave_event(session: AsyncSession, *, row: RemnawaveWebhookEvent, settings: Settings) -> None:
    payload = row.payload or {}
    event_type = (row.event_type or "").strip().lower()
    user_tg_id = telegram_id_from_payload(payload)
    user = None
    if user_tg_id is not None:
        user = (
            await session.execute(select(User).where(User.telegram_id == user_tg_id).limit(1))
        ).scalar_one_or_none()
    if user is None:
        rw_uuid = remnawave_user_uuid_from_payload(payload)
        if rw_uuid is not None:
            user = (
                await session.execute(select(User).where(User.remnawave_uuid == rw_uuid).limit(1))
            ).scalar_one_or_none()
    if user is None:
        row.status = "ignored"
        row.processed_at = datetime.now(timezone.utc)
        return

    now = datetime.now(timezone.utc)
    if event_type == "traffic.gb_step":
        if settings.billing_traffic_rw_meter_enabled:
            row.status = "ignored_meter_poll"
            row.processed_at = now
            await session.flush()
            return
        if not applies_pay_per_use_charges(user, settings):
            row.status = "ignored"
            row.processed_at = now
            await session.flush()
            return
        is_mobile = bool(payload.get("mobile_internet", False))
        ok = await charge_gb_step(
            session,
            user=user,
            event_id=row.event_id,
            event_ts=now,
            is_mobile_internet=is_mobile,
            settings=settings,
        )
        row.status = "processed" if ok else "rejected"
    elif event_type in (
        "device.attached",
        "device.detached",
        "user_hwid_devices.added",
        "user_hwid_devices.deleted",
    ):
        hwid = device_hwid_from_payload(payload)
        if not hwid:
            row.status = "ignored"
        else:
            is_active = event_type in ("device.attached", "user_hwid_devices.added")
            hist_count = (
                await session.execute(
                    select(func.count()).select_from(DeviceHistory).where(DeviceHistory.user_id == user.id)
                )
            ).scalar_one()
            first_ever_device = int(hist_count or 0) == 0
            hist_meta = {"source_event_id": row.event_id, **device_identity_meta_from_payload(payload)}
            await add_device_history_event(
                session,
                user_id=user.id,
                subscription_id=None,
                device_hwid=hwid,
                event_type=event_type,
                event_ts=now,
                is_active=is_active,
                meta=hist_meta,
            )
            if is_active and applies_pay_per_use_charges(user, settings):
                await charge_daily_device_once(
                    session,
                    user=user,
                    device_hwid=hwid,
                    day=billing_today(settings),
                    settings=settings,
                )
            if is_active:
                from shared.services.device_telegram_notify import notify_device_attached_replace_message

                await notify_device_attached_replace_message(
                    session, user, settings, first_ever=first_ever_device
                )
            row.status = "processed"
    elif event_type == "subscription.status":
        row.status = "processed"
    else:
        row.status = "ignored"

    row.processed_at = datetime.now(timezone.utc)
    await session.flush()


def parse_webhook_payload(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def event_id_from_payload(payload: dict, *, fallback: str) -> str:
    raw = payload.get("event_id") or payload.get("id") or fallback
    event_id = str(raw).strip() or str(fallback).strip() or "fallback"
    # Ограничиваем до размера колонки БД (String(128)), чтобы не ловить ошибки на flush/commit.
    return event_id[:128]


async def process_remnawave_event_by_id(*, event_id: str, settings: Settings) -> None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(RemnawaveWebhookEvent).where(RemnawaveWebhookEvent.event_id == event_id).limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return
        try:
            await process_remnawave_event(session, row=row, settings=settings)
        except Exception as exc:
            row.status = "error"
            row.error = str(exc)
            row.processed_at = datetime.now(timezone.utc)
        await session.commit()
