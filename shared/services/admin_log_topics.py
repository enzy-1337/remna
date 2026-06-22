"""Темы форума для админ-лога (ADMIN_LOG_TOPIC_*)."""

from __future__ import annotations

from enum import Enum


class AdminLogTopic(str, Enum):
    """Ключи для выбора message_thread_id в супергруппе с темами."""

    GENERAL = "general"
    PAYMENTS = "payments"
    USERS = "users"
    TRIALS = "trials"
    BONUSES = "bonuses"
    SUBSCRIPTIONS = "subscriptions"
    PROMO = "promo"
    DEVICES = "devices"
    SUPPORT = "support"
    LOGIN = "login"
    BACKUPS = "backups"
    REPORTS = "reports"
    BOOT = "boot"
    CHANNEL = "channel"
    ERRORS = "errors"
