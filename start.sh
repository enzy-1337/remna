#!/usr/bin/env bash
# Запуск всего стека (Linux/macOS): PostgreSQL, Redis, бот, API.
#   ./start.sh              — up -d --build (миграции через сервис migrate при up)
#   ./start.sh --migrate    — только alembic: docker compose run --rm migrate
#   ./start.sh --phase1-check — только preflight Phase 1
#   ./start.sh --foreground — логи в консоли
#   ./start.sh --no-build   — без пересборки
#   ./start.sh --down       — docker compose down

set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "Ошибка: нет .env — скопируйте .env.example в .env"
  exit 1
fi

if ! docker version >/dev/null 2>&1; then
  echo "Ошибка: Docker недоступен"
  exit 1
fi

DOWN=false
MIGRATE=false
PHASE1_CHECK=false
FOREGROUND=false
NO_BUILD=false

for a in "$@"; do
  case "$a" in
    --down) DOWN=true ;;
    --migrate) MIGRATE=true ;;
    --phase1-check) PHASE1_CHECK=true ;;
    --foreground) FOREGROUND=true ;;
    --no-build) NO_BUILD=true ;;
  esac
done

if [[ "$DOWN" == true ]]; then
  echo ">>> Остановка стека..."
  docker compose down
  exit 0
fi

if [[ "$MIGRATE" == true ]]; then
  echo ">>> Миграции БД (alembic upgrade head)..."
  docker compose run --rm migrate
fi

if [[ "$PHASE1_CHECK" == true ]]; then
  echo ">>> Preflight-проверка Phase 1..."
  docker compose run --rm bot python -m scripts.release_check_phase1
  exit 0
fi

ARGS=(compose up)
[[ "$FOREGROUND" == false ]] && ARGS+=(-d)
[[ "$NO_BUILD" == false ]] && ARGS+=(--build)

echo ">>> Запуск: docker ${ARGS[*]}"
docker "${ARGS[@]}"

if [[ "$FOREGROUND" == false ]]; then
  echo ""
  echo "Сервисы запущены."
  echo "  API: http://localhost:8000/health"
  echo "  Логи: docker compose logs -f bot"
fi
