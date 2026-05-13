#!/usr/bin/env bash
# flux_init — одна команда для подъёма стека и доступа к «файлам загрузки»
# (скрипты старта Docker, entrypoint, start.sh, пример .env).
#
# Использование:
#   ./scripts/flux_init.sh              — то же, что scripts/docker-stack-start.sh (postgres → migrate → все сервисы)
#   ./scripts/flux_init.sh --edit       — открыть файлы загрузки в $EDITOR (или nano)
#   ./scripts/flux_init.sh --list       — только список путей
#
# Удобный alias (добавьте в ~/.bashrc на VPS):
#   alias flux_init='/opt/remna-bot/scripts/flux_init.sh'

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STACK_START="$ROOT/scripts/docker-stack-start.sh"

LOAD_FILES=(
  "$STACK_START"
  "$ROOT/scripts/bot-entrypoint.sh"
  "$ROOT/start.sh"
  "$ROOT/.env.example"
  "$ROOT/docker-compose.yml"
)

case "${1:-}" in
  --edit|-e)
    paths=()
    for f in "${LOAD_FILES[@]}"; do
      [[ -f "$f" ]] && paths+=("$f")
    done
    if [[ ${#paths[@]} -eq 0 ]]; then
      echo "flux_init: не найдено ни одного из ожидаемых файлов." >&2
      exit 1
    fi
    "${EDITOR:-nano}" "${paths[@]}"
    ;;
  --list|-l)
    for f in "${LOAD_FILES[@]}"; do
      [[ -f "$f" ]] && printf '%s\n' "$f"
    done
    ;;
  --help|-h)
    cat <<'EOF'
flux_init — подъём Docker-стека и работа с файлами загрузки.

  ./scripts/flux_init.sh           Поднять стек: postgres → redis → migrate → bot, api, tickets-bot, downloader-bot, idbot
  ./scripts/flux_init.sh --edit    Открыть существующие файлы загрузки в $EDITOR
  ./scripts/flux_init.sh --list    Список путей к этим файлам
  ./scripts/flux_init.sh --help    Эта справка

На сервере удобно добавить в ~/.bashrc:
  alias flux_init='/opt/remna-bot/scripts/flux_init.sh'
EOF
    ;;
  "")
    if [[ ! -x "$STACK_START" ]]; then
      chmod +x "$STACK_START" 2>/dev/null || true
    fi
    exec bash "$STACK_START"
    ;;
  *)
    echo "flux_init: неизвестный аргумент «$1». См. ./scripts/flux_init.sh --help" >&2
    exit 1
    ;;
esac
