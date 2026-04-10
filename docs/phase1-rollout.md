# Phase 1 Rollout Runbook

Документ для безопасного включения гибридного биллинга (pay-per-use + monthly packages) в продакшене.

## 1. Подготовка

- Сделать резервную копию БД.
- Применить миграции:
  - `alembic upgrade head`
- Запустить preflight-проверку:
  - `python -m scripts.release_check_phase1`
  - или `./start.sh --phase1-check`
- Проверить, что в БД появились:
  - `billing_usage_events`
  - `billing_ledger_entries`
  - `billing_daily_summary`
  - `device_history`
  - `remnawave_webhook_events`
- Проверить новые поля:
  - `users.billing_mode`, `users.lifetime_exempt_flag`, `users.risk_notified_24h_at`, `users.risk_notified_1h_at`
  - `users.referral_bonus_message_id`, `users.device_notify_message_id`
  - `plans.device_limit`, `plans.monthly_gb_limit`, `plans.is_package_monthly`

## 2. Конфиг (минимум)

```env
BILLING_V2_ENABLED=false
REMNAWAVE_WEBHOOKS_ENABLED=false
REMNAWAVE_WEBHOOK_SECRET=<secret>
REMNAWAVE_SYNC_RUN_IMMEDIATELY=false
```

Рекомендуемые значения:

```env
BILLING_BALANCE_FLOOR_RUB=-50
BILLING_MIN_TOPUP_RUB=1
BILLING_DEVICE_DAILY_RUB=2.5
BILLING_GB_STEP_RUB=5
BILLING_MOBILE_GB_EXTRA_RUB=2.5
```

## 3. Пошаговое включение

1) Включить только webhook-приём:
- `REMNAWAVE_WEBHOOKS_ENABLED=true`
- `BILLING_V2_ENABLED=false`

Проверить:
- `/webhooks/remnawave` принимает валидные события;
- в `remnawave_webhook_events` есть записи;
- нет всплеска `invalid_signature`.

2) Включить Billing v2 только для новых пользователей:
- `BILLING_V2_ENABLED=true`
- `BILLING_V2_FOR_NEW_USERS_ONLY=true`

Проверить:
- у новых пользователей `billing_mode=hybrid`;
- списания идут в `billing_ledger_entries`;
- детализация в боте/админке не пустая.
- у пользователей `billing_mode=legacy` события вебхука трафика/устройств **не** создают списаний (`traffic.gb_step` → `ignored` в логе событий; дневная плата за устройство не начисляется), история HWID при этом пишется;
- `/start` **не** переводит старых legacy в hybrid (пока флаг true).

3) Запустить полный режим:
- `BILLING_V2_FOR_NEW_USERS_ONLY=false`

Проверить:
- legacy-переходы происходят автоматически после expiry;
- исключения (admin/lifetime) не переводятся.

## 4. Smoke checklist

- Webhook duplicate не вызывает повторных списаний.
- Invalid signature отклоняется (401) и фиксируется в событиях.
- Покупка тарифа:
  - подтверждение работает,
  - double click не вызывает двойного списания.
- Платный слот устройства:
  - подтверждение работает,
  - double click не вызывает двойного списания.
- Пакетный тариф:
  - до лимита списания нет,
  - после лимита списания есть.
- Уведомления риска минуса:
  - 24ч/1ч отправляются один раз на окно,
  - метки уведомлений обновляются.

## 5. Наблюдаемость

На dashboard web-admin отслеживать:
- webhook ok / duplicate / invalid (24ч),
- rating events (24ч),
- ledger rejects (24ч),
- transitions (24ч),
- risk 1ч / 24ч.

## 6. Rollback

Если аномалия в списаниях:

1) Немедленно отключить:
- `BILLING_V2_ENABLED=false`
- `REMNAWAVE_WEBHOOKS_ENABLED=false`

2) Оставить только legacy billing до анализа.

3) Проверить последние записи:
- `billing_ledger_entries`
- `transactions`
- `remnawave_webhook_events`

4) После исправления включать снова по шагам из раздела 3.

