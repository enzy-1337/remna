"""Передача подписки другому пользователю."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings
from shared.integrations.remnawave import RemnaWaveClient, RemnaWaveError
from shared.models.subscription import Subscription
from shared.models.subscription_transfer import SubscriptionTransfer
from shared.models.user import User
from shared.services.family_service import clear_family_for_owner
from shared.services.subscription_service import get_active_subscription

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def transfer_subscription(
    session: AsyncSession,
    *,
    settings: Settings,
    sender: User,
    recipient: User,
) -> tuple[bool, str]:
    if sender.id == recipient.id:
        return False, "Нельзя передать подписку самому себе."
    from shared.services.family_service import get_family_membership

    if await get_family_membership(session, user_id=sender.id) is not None:
        return False, "Передать подписку может только её владелец."

    sub = await get_active_subscription(session, sender.id, account_scope=False)
    if sub is None:
        return False, "Нет активной подписки для передачи."

    now = datetime.now(timezone.utc)
    remaining = _as_utc(sub.expires_at) - now
    if remaining.total_seconds() <= 0:
        return False, "Подписка уже истекла."

    rw = RemnaWaveClient(settings)
    sender_uuid = str(sender.remnawave_uuid) if sender.remnawave_uuid else None

    try:
        if sender_uuid:
            await rw.delete_all_user_hwid_devices(sender_uuid)

        recipient_sub = await get_active_subscription(session, recipient.id, account_scope=False)

        if recipient_sub is not None and recipient_sub.id != sub.id:
            recipient_sub.expires_at = _as_utc(recipient_sub.expires_at) + remaining
            recipient_sub.devices_count = max(int(recipient_sub.devices_count), int(sub.devices_count))
            sub.status = "cancelled"
            target_sub = recipient_sub
            target_user = recipient
        else:
            sub.user_id = recipient.id
            target_sub = sub
            target_user = recipient

        await clear_family_for_owner(session, owner_user_id=sender.id)
        await session.flush()

        if target_user.remnawave_uuid is None and sender.remnawave_uuid is not None:
            target_user.remnawave_uuid = sender.remnawave_uuid
            sender.remnawave_uuid = None
        elif target_user.remnawave_uuid is not None and sender.remnawave_uuid is not None:
            if sender_uuid:
                await rw.delete_all_user_hwid_devices(sender_uuid)

        panel_uuid = str(target_user.remnawave_uuid) if target_user.remnawave_uuid else None
        if panel_uuid:
            from shared.services.subscription_service import update_rw_user_respecting_hwid_limit

            await update_rw_user_respecting_hwid_limit(
                rw,
                panel_uuid,
                devices_limit_for_panel=int(target_sub.devices_count),
                expire_at=_as_utc(target_sub.expires_at),
                status="ACTIVE",
            )
            await rw.reset_user_subscription_credentials(panel_uuid, revoke_only_passwords=False)

        session.add(
            SubscriptionTransfer(
                from_user_id=sender.id,
                to_user_id=recipient.id,
                subscription_id=sub.id,
            )
        )
        await session.flush()
    except RemnaWaveError as e:
        logger.exception("transfer_subscription Remnawave failed sender=%s recipient=%s", sender.id, recipient.id)
        return False, f"Панель VPN недоступна: {e}"
    except Exception:
        logger.exception("transfer_subscription failed sender=%s recipient=%s", sender.id, recipient.id)
        return False, "Не удалось передать подписку. Попробуйте позже."

    return True, f"Подписка передана. Действует до {_as_utc(target_sub.expires_at).strftime('%d.%m.%Y %H:%M')} UTC."
