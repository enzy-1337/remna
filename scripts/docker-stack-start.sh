#!/usr/bin/env bash
# Надёжный подъём стека после reboot и при «network ... not found»:
# снимает compose-стек (контейнеры + сеть проекта), затем поднимает заново.
# Вызывается из systemd remna-bot.service или вручную из каталога приложения.
#
# Не трогает том pgdata (volume сохраняется при docker compose down без -v).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

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
"${COMPOSE[@]}" up -d bot api tickets-bot downloader-bot music-bot idbot

log "Stack status:"
"${COMPOSE[@]}" ps
