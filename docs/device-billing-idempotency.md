# Идемпотентность: суточное списание за устройство (фаза 2)

**Дата:** 2026-04-12  
**Связь:** [payg-vpn-system-master-plan.md §2.7](./payg-vpn-system-master-plan.md).

## Ключ списания за календарный день

Списание **не чаще одного раза за сутки** (в смысле `BILLING_CALENDAR_TIMEZONE`) на пару:

`(user_id, device_hwid)`

Реализация: `BillingUsageEvent.event_id` =

`device_daily:{user_id}:{device_hwid}:{YYYY-MM-DD}`

где дата — **локальный календарный день** биллинга, переданный в `charge_daily_device_once` как `day` (из `billing_today(settings)` в вебхуке или из батча за конкретный `local_day`).

Повторный `device.attached` / `user_hwid_devices.added` в тот же день с тем же HWID вызывает `charge_daily_device_once` снова, но вторая попытка находит уже существующий `BillingUsageEvent` с тем же `event_id` и **возвращает успех без повторного списания**.

## Дополнительные поля из вебхука (аудит)

В `DeviceHistory.meta` пишутся `source_event_id` и опционально имя/модель/uuid из payload (`device_identity_meta_from_payload` в `webhook_ingress_service`). Они **не участвуют** в ключе списания; при появлении в панели стабильного «логического id» устройства можно расширить документ и при необходимости миграцию ключей (смена формата `event_id` потребует аккуратного перехода).

## Батч полуночи

`device_daily_batch_service` вызывает тот же `charge_daily_device_once` с тем же форматом `event_id` — повтор за уже обработанный день идемпотентен.
