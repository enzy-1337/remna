#!/usr/bin/env bash
# Надёжный подъём стека после reboot и при «network ... not found»:
# снимает compose-стек (контейнеры + сеть проекта), затем поднимает заново.
# Вызывается из systemd remna-bot.service или вручную из каталога приложения.
#
# Не трогает том pgdata (volume сохраняется при docker compose down без -v).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

COMPOSE=(docker compose)

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

docker_down_soft() {
  "${COMPOSE[@]}" down --remove-orphans 2>/dev/null || true
}

docker_down_soft
log "Starting postgres and redis..."
"${COMPOSE[@]}" up -d postgres redis

log "Running database migrations..."
"${COMPOSE[@]}" run --rm migrate

log "Starting application services..."
APP_SERVICES=(bot api tickets-bot)
if [[ -n "${DOWNLOADER_BOT_TOKEN:-}" ]] && [[ -n "${DOWNLOADER_FORUM_CHAT_ID:-}" ]]; then
  APP_SERVICES+=(downloader-bot)
else
  log "Skipping downloader-bot (DOWNLOADER_BOT_TOKEN or DOWNLOADER_FORUM_CHAT_ID not set)"
fi
if [[ -n "${MUSIC_BOT_TOKEN:-}" ]] && [[ -n "${MUSIC_FORUM_CHAT_ID:-}" ]]; then
  APP_SERVICES+=(music-bot)
else
  log "Skipping music-bot (MUSIC_BOT_TOKEN or MUSIC_FORUM_CHAT_ID not set)"
fi
if [[ -n "${IDBOT_BOT_TOKEN:-}" ]]; then
  APP_SERVICES+=(idbot)
else
  log "Skipping idbot (IDBOT_BOT_TOKEN not set)"
fi
"${COMPOSE[@]}" up -d "${APP_SERVICES[@]}"

log "Stack status:"
"${COMPOSE[@]}" ps
