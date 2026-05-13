#!/usr/bin/env bash
# flux_init — запуск интерактивного меню управления стеком (deploy/remna-manager.sh)
# и вспомогательные режимы.
#
# Рекомендуемый alias на VPS (~/.bashrc):
#   alias flux_init='/opt/remna-bot/scripts/flux_init.sh'
# или из корня репозитория: ./flux_init

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANAGER="$ROOT/deploy/remna-manager.sh"
STACK_START="$ROOT/scripts/docker-stack-start.sh"

LOAD_FILES=(
  "$STACK_START"
  "$ROOT/scripts/bot-entrypoint.sh"
  "$ROOT/start.sh"
  "$ROOT/.env.example"
  "$ROOT/docker-compose.yml"
  "$MANAGER"
)

_run_menu() {
  if [[ ! -f "$MANAGER" ]]; then
    echo "flux_init: не найден $MANAGER" >&2
    exit 1
  fi
  chmod +x "$MANAGER" 2>/dev/null || true
  export APP_DIR="$ROOT"
  # Быстрый вход: сразу меню (запуск/остановка/логи/systemd и т.д.)
  exec bash "$MANAGER" flux-menu
}

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
  --stack|--up|-s)
    if [[ ! -x "$STACK_START" ]]; then
      chmod +x "$STACK_START" 2>/dev/null || true
    fi
    exec bash "$STACK_START"
    ;;
  --full|-f)
    chmod +x "$MANAGER" 2>/dev/null || true
    export APP_DIR="$ROOT"
    exec bash "$MANAGER"
    ;;
  --help|-h)
    cat <<'EOF'
flux_init — меню управления Docker-стеком (Remna).

  flux_init                  Интерактивное меню: запуск, остановка, логи, миграции, systemd…
  flux_init --full           Полный сценарий manager: выбор языка + проверка обновлений + меню
  flux_init --stack          Только подъём стека (scripts/docker-stack-start.sh)
  flux_init --edit           Открыть в $EDITOR ключевые файлы загрузки и deploy/remna-manager.sh

Переменные:
  FLUX_LANG=ru|en            Язык меню при быстром входе (по умолчанию ru)

Пример alias:
  alias flux_init='/opt/remna-bot/scripts/flux_init.sh'
EOF
    ;;
  "")
    _run_menu
    ;;
  *)
    echo "flux_init: неизвестный аргумент «$1». См. flux_init --help" >&2
    exit 1
    ;;
esac
