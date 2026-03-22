-- Напоминания об окончании подписки (за ~24 ч и ~3 ч). Выполнить один раз на PostgreSQL.
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS expiry_notified_24h boolean NOT NULL DEFAULT false;
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS expiry_notified_3h boolean NOT NULL DEFAULT false;
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS expiry_notify_anchor_at timestamp with time zone NULL;
