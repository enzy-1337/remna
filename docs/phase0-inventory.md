# Фаза 0 — инвентаризация кода (pay-as-you-go / remna)

**Дата:** 2026-04-12  
**Панель (прод):** Remnawave **2.7.4** (зафиксировано владельцем).  
**Связанные документы:** [payg-vpn-system-master-plan.md](./payg-vpn-system-master-plan.md), [phase1-rollout.md](./phase1-rollout.md).

---

## 1. Цель этого файла

Зафиксировать **текущее состояние** репозитория относительно мастер-плана: какие модули за что отвечают, что уже покрывает фазы 1–5 частично, где **разрыв** с требованиями §2.9 (модель ГБ «предоплата блока»), «оптимизированный маршрут», калькулятор в боте и т.д.

---

## 2. Карта `shared/services/billing_v2/`

| Файл | Назначение |
|------|------------|
| `charging_policy.py` | `applies_pay_per_use_charges()`: списания только если `BILLING_V2_ENABLED` и `user.billing_mode == "hybrid"`. |
| `webhook_ingress_service.py` | HMAC, `store_raw_webhook_event`, `process_remnawave_event`: типы `traffic.gb_step`, device attach/detach, `user_hwid_devices.*`, `subscription.status`; резолв пользователя по `telegram_id` или `remnawave_uuid`; `device_identity_meta_from_payload` → `DeviceHistory.meta`. |
| `rating_service.py` | `charge_gb_step`, `charge_daily_device_once`; пакетное покрытие по `Plan.is_package_monthly` + лимиты `monthly_gb_limit` / `device_limit`; идемпотентность `gb:{event_id}` и `device:{user}:{hwid}:{day}`. |
| `ledger_service.py` | `apply_debit`: пол баланса `BILLING_BALANCE_FLOOR_RUB`, дубликаты по `idempotency_key`, запись `BillingLedgerEntry` + `Transaction`. |
| `detail_service.py` | `BillingDailySummary` за день/месяц, `usage_package_breakdown` по `BillingUsageEvent` (покрыто пакетом vs списано). |
| `billing_calendar.py` | Календарный день биллинга (`BILLING_CALENDAR_TIMEZONE`); границы месяца для пакетного лимита ГБ — `billing_package_month_utc_bounds`. |
| `device_service.py` | История устройств (`DeviceHistory`), активные HWID для пакета. |
| `device_daily_midnight_loop.py` / `device_daily_batch_service.py` | Догоняющие/полночные сценарии по устройствам (см. код при необходимости фазы 2). |
| `transition_service.py` | Перевод `legacy` → `hybrid` после expiry, исключения админов/`lifetime_exempt`/`billing_legacy_lifetime_cutoff_year`. |
| `negative_balance_notify_loop.py` | Уведомления риска минуса (связка с полом баланса). |
| `cleanup_loop.py` | Уборка старых деталей по retention. |

---

## 3. Модели и таблицы (релевантные плану)

- **`plans` (`Plan`)**: `device_limit`, `monthly_gb_limit`, `is_package_monthly`, `traffic_limit_gb`, цена/длительность — база пакетов и калькулятора админки.
- **`subscriptions` (`Subscription`)**: связь с планом, статусы `active|trial|expired|cancelled`, `expires_at`, `auto_renew`.
- **`users`**: `balance`, `billing_mode` (`legacy` / `hybrid`), флаги риска/уведомлений (см. миграции и `phase1-rollout.md`).
- **`billing_usage_events`**: факт использования; типы минимум `traffic_gb_step`, `device_daily`; `event_id` уникален для идемпотентности вебхуков.
- **`billing_ledger_entries`**, **`billing_daily_summary`**, **`remnawave_webhook_events`**, **`device_history`**: как в runbook phase1.

---

## 4. Вебхуки Remnawave (факт кода)

Обрабатываются в `process_remnawave_event`:

| `event_type` (нижний регистр) | Действие |
|-------------------------------|----------|
| `traffic.gb_step` | При hybrid+v2: `charge_gb_step` (учёт пакета по числу шагов ГБ в календарном месяце UTC-границ месяца от `event_ts` — см. `rating_service._month_bounds`). |
| `device.attached`, `user_hwid_devices.added` | История + при hybrid: `charge_daily_device_once` за `billing_today`; уведомление в Telegram о устройстве. |
| `device.detached`, `user_hwid_devices.deleted` | История, без дневного списания при detach. |
| `subscription.status` | Помечается `processed`, без биллинга в этом файле. |
| Прочее | `ignored`. |

**Идентификация пользователя:** `telegram_id_from_payload` / `remnawave_user_uuid_from_payload`.  
**HWID:** `device_hwid_from_payload` (в т.ч. `data.hwidUserDevice.hwid`).

Для **2.7.4** нужно в фазе 1 сверить с официальным контрактом вебхуков (Q1 в мастер-плане): совпадает ли семантика шага ГБ с §2.9 мастер-плана.

---

## 5. Детализация расходов (уже есть частично)

| Место | Реализация |
|-------|------------|
| **Бот** | `bot/handlers/subscription.py`: «Детализация» → сегодня / месяц; `billing_today`, `get_today_summary`, `get_month_summaries`, `usage_package_breakdown`. |
| **Web-admin** | Агрегаты по пользователю — смотреть `api/routers/web_admin.py` (профиль, биллинг); не дублировать здесь полный список экранов. |
| **Тикеты** | `api/routers/tickets_api.py`: `GET /billing/{user_id}/detail/today` и `.../month` для виджетов/интеграции (календарь из `billing_today(settings)`). |

План фазы 5 требует выровнять форматы и **явно зафиксировать таймзону** для «текущих суток» в тикетах — сейчас опора на `BILLING_CALENDAR_TIMEZONE` через `billing_today`.

---

## 6. Калькулятор и тарифы

- **`shared/services/billing_calculator.py`**: `estimate_pay_per_use_30d_rub`, `transition_credit_for_remaining_legacy_rub`, `plan_fields_for_ppu_estimate` — используется админскими сценариями; **отдельного UX «Калькулятор» в боте для пользователя** по плану фазы 8 — **пока нет**.
- **Тарифы в web-admin / боте**: покупка планов из БД есть; полный «конструктор» из фазы 10 — оценивать отдельно по экранам `web_admin`.

---

## 7. Сверка с [phase1-rollout.md](./phase1-rollout.md)

| Переключатель | Смысл в коде |
|---------------|--------------|
| `BILLING_V2_ENABLED` | Включает hybrid-списания через `applies_pay_per_use_charges`. |
| `BILLING_V2_FOR_NEW_USERS_ONLY` | Ограничение перехода/применения (см. `transition_service`, регистрацию пользователя). |
| `REMNAWAVE_WEBHOOKS_ENABLED` | Приём вебхуков на API (отдельно от биллинга — проверить `api/routers/webhooks.py`). |

Runbook описывает безопасное пошаговое включение; **фактические значения на проде** не хранятся в репозитории — зафиксировать у DevOps.

---

## 8. Разрывы относительно мастер-плана (кратко)

| Тема мастер-плана | Статус в коде |
|-------------------|---------------|
| §2.9 предоплата блока ГБ до 1 ГБ | Сейчас опора на **дискретные** `traffic.gb_step` от панели + пакетный overage по счётчику шагов; отдельного счётчика «оплаченных блоков вперёд» в БД **нет** — см. [billing-gb-state-machine.md](./billing-gb-state-machine.md). |
| §2.8 «Оптимизированный маршрут» (+2,5 ₽/ГБ, squad) | **Не реализовано** в просмотренных модулях (фаза 3). |
| §2.6 первое пополнение / бонусы | Частично может быть в topup/promo; отдельная параметризация из фазы 6 — проверить при реализации. |
| §2.3 калькулятор в боте | **Нет** (фаза 8). |
| §2.9 перевыпуск подписки одним API | Искать в `RemnaWaveClient` + зафиксировать эндпоинт под 2.7.4 (фаза 9). |

---

## 9. Следующий шаг (фаза 1)

1. Уточнить по документации **Remnawave 2.7.4**, когда именно шлётся `traffic.gb_step` (соответствие целевой state machine).  
2. **Сделано (часть, 2026-04-12):** месяц пакетного лимита ГБ по `BILLING_CALENDAR_TIMEZONE`; идемпотентность по `BillingUsageEvent.event_id` в `charge_gb_step`; для payg — сначала `apply_debit`, затем запись usage (нет события при отказе по полу); день `BillingDailySummary` для ГБ — дата в той же таймзоне.  
3. Осталось по фазе 1: явные счётчики «оплаченных блоков» при необходимости контракта панели.  
4. **Фаза 2 (часть 2026-04-12):** см. [device-billing-idempotency.md](./device-billing-idempotency.md); тесты `test_rating_device_daily_integration.py`. Дальше — фаза 3 («оптимизированный маршрут»).
