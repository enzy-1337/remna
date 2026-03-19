# Remna VPN Telegram Bot

Проект по спецификации Remnawave + Telegram (шаги 5–12).

## Требования

- Python 3.11+
- PostgreSQL 16+ (рекомендуется)
- Redis
- Токен бота, канал для обязательной подписки
- Панель Remnawave с API (или `REMNAWAVE_STUB=true` для тестов без панели)

## Установка

```bash
pip install -r requirements.txt
# или
pip install -e .
```

Скопируйте `.env.example` → `.env` и заполните переменные.

### Миграции БД

```bash
alembic upgrade head
```

Создаются все таблицы из ТЗ и начальные тарифы (Триал, 1/2/3 месяца).

## Запуск бота

Из корня репозитория:

```bash
python -m bot.main
```

### Интерфейс (фото + профиль)

- Главный экран — **«Профиль»** с фото (файл `bot/assets/section_header.png` или `BOT_SECTION_PHOTO_PATH` / `BOT_SECTION_PHOTO_URL` в `.env`).
- Переходы по разделам: предыдущее сообщение удаляется и отправляется новое с тем же стилем.
- Промокод: команда **`/promo`** (кнопки в профиле можно добавить позже).
- Инструкции: **`INSTRUCTION_TELEGRAPH_PHONE_URL`** и **`INSTRUCTION_TELEGRAPH_PC_URL`** (при отсутствии используются старые `INSTRUCTION_*_URL`).

Бот должен иметь доступ к `getChatMember` по каналу (часто нужны права администратора бота в канале).

## API (вебхуки платежей)

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

- `POST /webhooks/cryptobot` — Crypto Pay (@CryptoBot), подпись `crypto-pay-api-signature`.
- `POST /webhooks/platega` — Platega callback (проверка заголовков `X-MerchantId` / `X-Secret`).
- `GET /health`

В кабинете Crypto Bot и Platega укажите публичный HTTPS URL этих путей.

## Реализованные шаги

- **Шаг 5** — middleware обязательной подписки на канал + кэш Redis.
- **Шаг 6** — PostgreSQL + модели, регистрация, deep-link `ref_CODE`, главное меню (inline), триал (3 дня / лимит ГБ из `.env`), `RemnaWaveClient` (`POST /api/users`, ссылка подписки), `MAINTENANCE_MODE`.
- **Шаг 7** — **Strategy**: `shared/payments/` (`BasePaymentProvider`, CryptoBot, Platega), пополнение в боте (суммы 100–500 / ручной ввод, FSM), pending-транзакции + идемпотентные вебхуки, зачисление на баланс, уведомление в Telegram, «умная корзина» в Redis (`smart_cart:{telegram_id}`).
- **Шаг 8** — покупка/продление тарифа с баланса (синхронизация Remnawave, продление от `max(now, expires_at)`), при нехватке средств — запись в корзину и **авто-покупка после пополнения** (`try_apply_smart_cart_after_topup`), устройства (2–10 слотов, платное добавление, `EXTRA_DEVICE_PRICE_RUB`), экраны «Моя подписка» и «Устройства» (`bot/handlers/subscription.py`, `devices.py`), `subscription_service` + `RemnaWaveClient.update_user`.
- **Шаг 9** — рефералы: deep-link `ref_CODE` (регистрация), **однократная награда пригласившему** при первой **платной** покупке приглашённого (`grant_referrer_reward_first_paid_plan`, запись `referral_rewards`, транзакция `referral_reward`), опционально **+дни** к активной подписке пригласившего + синхронизация Remnawave; уникальный частичный индекс `0002_referral_uq`; экран «Рефералы» (`bot/handlers/referrals.py`), переменные `REFERRAL_INVITER_BONUS_RUB` / `REFERRAL_INVITER_BONUS_DAYS`.
- **Шаг 10** — промокоды: экран ввода в боте (`bot/handlers/promo.py`, FSM), применение в `promo_service` с проверками `is_active`/`expires_at`/`max_uses`, запрет повторного использования одним пользователем (`0003_promo_usage_uq`), начисление в `balance` или `bonus_balance` (типы `balance_rub` / `bonus_rub`) и запись транзакции.
- **Шаг 11** — инструкции: экран «📖 Инструкции» теперь берёт ссылки из `.env` (`INSTRUCTION_ANDROID_URL`, `INSTRUCTION_IOS_URL`, `INSTRUCTION_WINDOWS_URL`, `INSTRUCTION_MACOS_URL`) и показывает кнопки по платформам; если ссылки не заданы — бот подсказывает, что нужно заполнить переменные.
- **Шаг 12** — админ-уведомления: `ADMIN_LOG_CHAT_ID` / опционально `ADMIN_LOG_TOPIC_ID` (тема форума), сервис `shared/services/admin_notify.py` + расширенный `send_telegram_message` (`message_thread_id`). События: пополнение, покупка тарифа с баланса, триал, реферальный бонус, промокод, покупка слота устройства. Дублирование в `notifications_log` (`type=admin:*`, статусы `sent` / `failed` / `skipped_no_admin_chat`).

## Переменные Remnawave

- `REMNAWAVE_API_URL` — базовый URL панели (без `/api` в конце).
- `REMNAWAVE_API_TOKEN` — JWT для `Authorization: Bearer`.
- `REMNAWAVE_DEFAULT_SQUAD_UUID` — UUID internal squad (массив `activeInternalSquads`).
- `REMNAWAVE_COOKIE` — при необходимости cookie `__remnawave-reverse-proxy__`.
- `REMNAWAVE_STUB=true` — не вызывать API (пустой токен допустим).
