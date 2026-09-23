"""Рассылка по почте (реклама/новости) — только пользователям с согласием email_marketing_consent.

Чеки о пополнении, письма о продлении и напоминания об окончании подписки — транзакционные,
отправляются всегда и от согласия не зависят (см. subscription_email_notify.py).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings, get_settings
from shared.database import get_session_factory
from shared.models.user import User
from shared.services.email_sender import send_branded_email, site_url

logger = logging.getLogger(__name__)

_UNSUB_SALT = "flux-email-unsubscribe"


def set_marketing_consent(user: User, consent: bool) -> None:
    user.email_marketing_consent = bool(consent)
    user.email_marketing_consent_at = datetime.now(timezone.utc) if consent else None


def _unsub_serializer(settings: Settings) -> URLSafeSerializer:
    return URLSafeSerializer(str(settings.web_admin_session_secret), salt=_UNSUB_SALT)


def unsubscribe_url(user: User, settings: Settings) -> str | None:
    base = site_url(settings, "/email/unsubscribe")
    if not base or not user.email:
        return None
    token = _unsub_serializer(settings).dumps({"u": int(user.id), "e": user.email})
    return f"{base}?t={token}"


def parse_unsubscribe_token(token: str, settings: Settings | None = None) -> tuple[int, str] | None:
    s = settings or get_settings()
    try:
        data = _unsub_serializer(s).loads(token or "")
    except BadSignature:
        return None
    if not isinstance(data, dict) or "u" not in data or "e" not in data:
        return None
    return int(data["u"]), str(data["e"])


def _recipients_filter():
    return (
        User.email.is_not(None),
        User.email_verified_at.is_not(None),
        User.email_marketing_consent.is_(True),
        User.is_blocked.is_(False),
    )


async def count_marketing_recipients(session: AsyncSession) -> tuple[int, int]:
    """(с согласием, всего с подтверждённой почтой)."""
    consent = (await session.execute(select(func.count(User.id)).where(*_recipients_filter()))).scalar_one()
    total = (
        await session.execute(
            select(func.count(User.id)).where(User.email.is_not(None), User.email_verified_at.is_not(None))
        )
    ).scalar_one()
    return int(consent), int(total)


@dataclass
class EmailCampaign:
    subject: str
    title: str
    text: str
    button_text: str = ""
    button_url: str = ""
    kind: str = "info"  # info | promo


@dataclass
class CampaignProgress:
    running: bool = False
    subject: str = ""
    total: int = 0
    sent: int = 0
    failed: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str = ""
    started_by: str = ""
    cancel: bool = False
    errors: list[str] = field(default_factory=list)


# Одна рассылка за раз на процесс API — прогресс показывается на странице админки.
PROGRESS = CampaignProgress()
_TASK: asyncio.Task | None = None


def _badge(kind: str) -> str:
    return "Акция" if kind == "promo" else "Новости"


async def send_campaign_email(to: str, user: User | None, campaign: EmailCampaign, settings: Settings) -> tuple[bool, str]:
    return await send_branded_email(
        to,
        subject=campaign.subject,
        title=campaign.title or campaign.subject,
        intro=campaign.text,
        button_text=campaign.button_text or None,
        button_url=campaign.button_url or None,
        badge=_badge(campaign.kind),
        tone="accent",
        unsubscribe_url=(unsubscribe_url(user, settings) if user is not None else None)
        or site_url(settings, "/app/profile")
        or None,
        settings=settings,
    )


async def _run_campaign(campaign: EmailCampaign, settings: Settings, delay_sec: float) -> None:
    factory = get_session_factory()
    try:
        async with factory() as session:
            users = list(
                (await session.execute(select(User).where(*_recipients_filter()).order_by(User.id))).scalars()
            )
        PROGRESS.total = len(users)
        for user in users:
            if PROGRESS.cancel:
                PROGRESS.last_error = "Остановлено администратором"
                break
            ok, err = await send_campaign_email(user.email or "", user, campaign, settings)
            if ok:
                PROGRESS.sent += 1
            else:
                PROGRESS.failed += 1
                PROGRESS.last_error = err
                if len(PROGRESS.errors) < 20:
                    PROGRESS.errors.append(f"#{user.id} {user.email}: {err}")
                # Gmail при превышении дневного лимита отвечает 550/421 — дальше слать бессмысленно.
                if "5.4.5" in err or "Daily user sending limit" in err:
                    PROGRESS.last_error = "Gmail: превышен дневной лимит отправки. Продолжите завтра."
                    break
            await asyncio.sleep(delay_sec)
    except Exception as e:  # noqa: BLE001
        logger.exception("email campaign failed")
        PROGRESS.last_error = f"{type(e).__name__}: {e}"
    finally:
        PROGRESS.running = False
        PROGRESS.finished_at = datetime.now(timezone.utc)
        logger.info(
            "email campaign done: subject=%r sent=%s failed=%s", PROGRESS.subject, PROGRESS.sent, PROGRESS.failed
        )


def start_campaign(campaign: EmailCampaign, *, started_by: str = "", delay_sec: float = 1.5) -> bool:
    """False — уже идёт другая рассылка."""
    global PROGRESS, _TASK
    if PROGRESS.running:
        return False
    PROGRESS = CampaignProgress(
        running=True,
        subject=campaign.subject,
        started_at=datetime.now(timezone.utc),
        started_by=started_by,
    )
    _TASK = asyncio.get_running_loop().create_task(_run_campaign(campaign, get_settings(), delay_sec))
    return True


def cancel_campaign() -> None:
    if PROGRESS.running:
        PROGRESS.cancel = True
