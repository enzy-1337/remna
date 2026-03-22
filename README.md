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

## Фоновая синхронизация Remnawave

Пока бот работает, в фоне периодически подтягиваются пользователи из панели и обновляются локальные подписки для тех, у кого задан `remnawave_uuid`.

- Интервал: **`REMNAWAVE_SYNC_INTERVAL_SEC`** (по умолчанию **1800** сек = **30 минут**).
- В логах после каждого цикла: `RW sync: цикл завершён (... пользователей с remnawave_uuid)`.

### Напоминания о конце подписки

Бот шлёт личные сообщения (без разметки), когда до `expires_at` остаётся примерно **24 часа** и **3 часа** (`SUBSCRIPTION_EXPIRY_NOTIFY_*` в `.env`). После продления флаги сбрасываются автоматически.

Нужны колонки в `subscriptions` — см. `scripts/add_subscription_expiry_notify_columns.sql`.

## Запуск бота

Из корня репозитория:

```bash
python -m bot.main
```

### Docker (бот + API + PostgreSQL + Redis)

```bash
cp .env.example .env
# заполните BOT_TOKEN, REQUIRED_CHANNEL_*, REMNAWAVE_*, и т.д.

docker compose build
docker compose run --rm bot alembic upgrade head
docker compose up -d
```

Или одной командой из корня репозитория:

- **Windows:** `.\start.bat` или `.\start.ps1` (первый раз с миграциями: `.\start.ps1 -Migrate`)
- **Linux/macOS:** `chmod +x start.sh && ./start.sh` (с миграциями: `./start.sh --migrate`)

- Сервис **`bot`** — `python -m bot.main`
- Сервис **`api`** — `uvicorn api.main:app` на порту **8000** (вебхуки платежей)
- **Postgres** и **Redis** поднимаются автоматически; `DATABASE_URL` / `REDIS_URL` в compose переопределены под сеть Docker.
- Порты **5432** и **6379** на хост **не пробрасываются** (чтобы не конфликтовать с уже установленным PostgreSQL/Redis на VPS). Подключение к БД из контейнеров идёт по имени `postgres` / `redis`. Нужен доступ с хоста — см. `docker-compose.override.example.yml`.

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
- **Шаг 9** — рефералы: deep-link `ref_CODE` (регистрация), опционально **бонус пригласившему при регистрации друга** (`REFERRAL_SIGNUP_BONUS_RUB`, транзакция `referral_signup`), плюс **однократная награда пригласившему** при первой **платной** покупке приглашённого (`grant_referrer_reward_first_paid_plan`, запись `referral_rewards`, транзакция `referral_reward`), опционально **+дни** к активной подписке пригласившего + синхронизация Remnawave; уникальный частичный индекс `0002_referral_uq`; экран «Рефералы» (`bot/handlers/referrals.py`), переменные `REFERRAL_INVITER_BONUS_RUB` / `REFERRAL_INVITER_BONUS_DAYS`.
- **Шаг 10** — промокоды: экран ввода в боте (`bot/handlers/promo.py`, FSM), применение в `promo_service` с проверками `is_active`/`expires_at`/`max_uses`, запрет повторного использования одним пользователем (`0003_promo_usage_uq`), начисление в `balance` или `bonus_balance` (типы `balance_rub` / `bonus_rub`) и запись транзакции.
- **Шаг 11** — инструкции: экран «📖 Инструкции» теперь берёт ссылки из `.env` (`INSTRUCTION_ANDROID_URL`, `INSTRUCTION_IOS_URL`, `INSTRUCTION_WINDOWS_URL`, `INSTRUCTION_MACOS_URL`) и показывает кнопки по платформам; если ссылки не заданы — бот подсказывает, что нужно заполнить переменные.
- **Шаг 12** — админ-уведомления: `ADMIN_LOG_CHAT_ID` / опционально `ADMIN_LOG_TOPIC_ID` (тема форума), сервис `shared/services/admin_notify.py` + расширенный `send_telegram_message` (`message_thread_id`). События: пополнение, покупка тарифа с баланса, триал, реферальный бонус, промокод, покупка слота устройства. Дублирование в `notifications_log` (`type=admin:*`, статусы `sent` / `failed` / `skipped_no_admin_chat`).

## Переменные Remnawave

- `REMNAWAVE_API_URL` — **только origin** панели, например `https://panel.example.com` (без пути к эндпоинтам).
- `REMNAWAVE_API_PATH_PREFIX` — префикс на стороне nginx (по умолчанию **`/api`** → запросы вида `{origin}/api/users`). Если nginx отдаёт **404 HTML** на `POST .../api/users`, проверьте проксирование в панель или задайте другой префикс (например пустой строкой и уточните URL у хостинга).
- `REMNAWAVE_API_TOKEN` — JWT для `Authorization: Bearer`.
- `REMNAWAVE_DEFAULT_SQUAD_UUID` — UUID internal squad (массив `activeInternalSquads`).
- `REMNAWAVE_COOKIE` — при необходимости cookie `__remnawave-reverse-proxy__`.
- `REMNAWAVE_STUB=true` — не вызывать API (пустой токен допустим).

## Админ-панель в боте

- `ADMIN_TELEGRAM_ID` — один numeric Telegram user id.
- `ADMIN_TELEGRAM_IDS` — несколько id через запятую (дополнительно к `ADMIN_TELEGRAM_ID`).
- У таких пользователей в профиле внизу две кнопки: **Поддержка** и **Админ-панель** (список пользователей, блокировка, поиск по Telegram ID). Команда **`/admin`** открывает то же меню.

## Форматирование сообщений

Бот использует **MarkdownV2** (`ParseMode.MARKDOWN_V2`). Пользовательский текст экранируется в `shared/md2.py` (`esc`, `bold`, `code`, ссылки и т.д.).
