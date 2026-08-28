"""SQLAlchemy-модели (полная схема ТЗ)."""

from shared.models.admin_role import AdminRole
from shared.models.admin_user import AdminUser
from shared.models.base import Base
from shared.models.family_member import FamilyMember
from shared.models.subscription_transfer import SubscriptionTransfer
from shared.models.ticket_rating import TicketRating
from shared.models.broadcast_mailing import BroadcastHistory, BroadcastTemplate, ScheduledBroadcast
from shared.models.billing_cron_checkpoint import BillingCronCheckpoint
from shared.models.billing_daily_summary import BillingDailySummary
from shared.models.billing_ledger_entry import BillingLedgerEntry
from shared.models.billing_traffic_meter import BillingTrafficMeter
from shared.models.billing_usage_event import BillingUsageEvent
from shared.models.device import Device
from shared.models.device_history import DeviceHistory
from shared.models.downloader_user_topic import DownloaderUserTopic
from shared.models.fraud_detector_state import FraudDetectorState
from shared.models.fraud_incident import FraudIncident
from shared.models.ip_connection_sample import IpConnectionSample
from shared.models.music_user_topic import MusicUserTopic
from shared.models.notification_log import NotificationLog
from shared.models.plan import Plan
from shared.models.promo import PromoCode, PromoUsage
from shared.models.referral_reward import ReferralReward
from shared.models.remnawave_webhook_event import RemnawaveWebhookEvent
from shared.models.subscription import Subscription
from shared.models.telegram_blacklist_entry import TelegramBlacklistEntry
from shared.models.telegram_blacklist_sync_state import TelegramBlacklistSyncState
from shared.models.traffic_sample import TrafficSample
from shared.models.transaction import Transaction
from shared.models.user import User
from shared.models.user_fraud_state import UserFraudState
from shared.models.web_admin_browser_session import WebAdminBrowserSession

__all__ = [
    "AdminRole",
    "AdminUser",
    "Base",
    "FamilyMember",
    "SubscriptionTransfer",
    "TicketRating",
    "BroadcastTemplate",
    "ScheduledBroadcast",
    "BroadcastHistory",
    "BillingTrafficMeter",
    "BillingUsageEvent",
    "BillingLedgerEntry",
    "BillingCronCheckpoint",
    "BillingDailySummary",
    "DeviceHistory",
    "DownloaderUserTopic",
    "FraudDetectorState",
    "FraudIncident",
    "IpConnectionSample",
    "MusicUserTopic",
    "RemnawaveWebhookEvent",
    "TelegramBlacklistEntry",
    "TelegramBlacklistSyncState",
    "TrafficSample",
    "User",
    "UserFraudState",
    "WebAdminBrowserSession",
    "Plan",
    "Subscription",
    "Device",
    "Transaction",
    "ReferralReward",
    "PromoCode",
    "PromoUsage",
    "NotificationLog",
]
