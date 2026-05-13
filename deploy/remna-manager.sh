#!/usr/bin/env bash

set -euo pipefail

APP_NAME="remna-bot"
DEFAULT_APP_DIR="/opt/remna-bot"
DEFAULT_SERVICE_NAME="remna-bot.service"
DEFAULT_REPO_URL="https://github.com/your-org/remna-bot.git"
DOCKER_COMPOSE_FILE="docker-compose.yml"

LANG_CHOICE=""
APP_DIR="${APP_DIR:-$DEFAULT_APP_DIR}"
SERVICE_NAME="${SERVICE_NAME:-$DEFAULT_SERVICE_NAME}"
REPO_URL="${REPO_URL:-$DEFAULT_REPO_URL}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  SUDO=""
fi

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

msg() {
  local key="$1"
  case "${LANG_CHOICE}" in
    ru)
      case "$key" in
        choose_lang) echo "Выберите язык / Choose language:" ;;
        choose_lang_hint) echo "1) Русский  2) English" ;;
        invalid_choice) echo "Неверный выбор. Попробуйте снова." ;;
        app_dir_prompt) echo "Каталог установки [$APP_DIR]:" ;;
        repo_prompt) echo "URL репозитория [$REPO_URL]:" ;;
        installed_yes) echo "Система установлена." ;;
        installed_no) echo "Система не установлена." ;;
        offer_install) echo "Установить систему сейчас? (y/n):" ;;
        checking_updates) echo "Проверяю обновления..." ;;
        updates_found) echo "Доступны обновления." ;;
        updates_not_found) echo "Обновлений нет." ;;
        offer_update) echo "Обновить сейчас? (y/n):" ;;
        menu_title) echo "===== Меню управления Remna · flux_init =====" ;;
        menu_install) echo "1) Установить систему (первый запуск)" ;;
        menu_update) echo "2) Обновить систему" ;;
        menu_start) echo "3) Запустить стек" ;;
        menu_stop) echo "4) Остановить стек" ;;
        menu_restart) echo "5) Перезапустить стек" ;;
        menu_status) echo "6) Статус сервисов" ;;
        menu_logs) echo "7) Логи (all/сервис)" ;;
        menu_rebuild) echo "8) Пересобрать и поднять (force recreate)" ;;
        menu_migrate) echo "9) Прогнать миграции" ;;
        menu_fix_net) echo "10) Починить сеть Docker (network not found)" ;;
        menu_autostart) echo "11) Настроить автозапуск через systemd" ;;
        menu_diag) echo "12) Диагностика окружения" ;;
        menu_lang) echo "13) Сменить язык" ;;
        menu_exit) echo "0) Выход" ;;
        enter_choice) echo -n "Выберите действие: " ;;
        need_install_first) echo "Сначала установите систему (пункт 1)." ;;
        install_started) echo "Начинаю установку..." ;;
        install_done) echo "Установка завершена." ;;
        update_started) echo "Начинаю обновление..." ;;
        update_done) echo "Обновление завершено." ;;
        start_done) echo "Стек запущен." ;;
        stop_done) echo "Стек остановлен." ;;
        restart_done) echo "Стек перезапущен." ;;
        enter_service_name) echo -n "Введите имя сервиса (или Enter для всех): " ;;
        press_enter) echo -n "Нажмите Enter для продолжения..." ;;
        env_created) echo ".env создан из .env.example. Заполните переменные перед прод-использованием." ;;
        env_missing) echo "Не найден ни .env, ни .env.example. Создайте .env вручную." ;;
        autostart_done) echo "Автозапуск через systemd настроен." ;;
        only_linux_systemd) echo "Настройка systemd поддерживается только на Linux." ;;
        prereq_title) echo "Проверка и установка зависимостей..." ;;
        done) echo "Готово." ;;
        fail) echo "Ошибка." ;;
        current_dir_not_repo) echo "Текущий каталог не похож на директорию приложения." ;;
        *)
          echo "$key"
          ;;
      esac
      ;;
    *)
      case "$key" in
        choose_lang) echo "Choose language / Выберите язык:" ;;
        choose_lang_hint) echo "1) Русский  2) English" ;;
        invalid_choice) echo "Invalid choice. Try again." ;;
        app_dir_prompt) echo "Install directory [$APP_DIR]:" ;;
        repo_prompt) echo "Repository URL [$REPO_URL]:" ;;
        installed_yes) echo "System is installed." ;;
        installed_no) echo "System is not installed." ;;
        offer_install) echo "Install system now? (y/n):" ;;
        checking_updates) echo "Checking for updates..." ;;
        updates_found) echo "Updates are available." ;;
        updates_not_found) echo "No updates found." ;;
        offer_update) echo "Update now? (y/n):" ;;
        menu_title) echo "===== Remna Control Menu · flux_init =====" ;;
        menu_install) echo "1) Install system (first run)" ;;
        menu_update) echo "2) Update system" ;;
        menu_start) echo "3) Start stack" ;;
        menu_stop) echo "4) Stop stack" ;;
        menu_restart) echo "5) Restart stack" ;;
        menu_status) echo "6) Services status" ;;
        menu_logs) echo "7) Logs (all/service)" ;;
        menu_rebuild) echo "8) Rebuild and up (force recreate)" ;;
        menu_migrate) echo "9) Run migrations" ;;
        menu_fix_net) echo "10) Fix Docker network (network not found)" ;;
        menu_autostart) echo "11) Setup systemd autostart" ;;
        menu_diag) echo "12) Environment diagnostics" ;;
        menu_lang) echo "13) Change language" ;;
        menu_exit) echo "0) Exit" ;;
        enter_choice) echo -n "Choose action: " ;;
        need_install_first) echo "Install the system first (option 1)." ;;
        install_started) echo "Starting installation..." ;;
        install_done) echo "Installation completed." ;;
        update_started) echo "Starting update..." ;;
        update_done) echo "Update completed." ;;
        start_done) echo "Stack started." ;;
        stop_done) echo "Stack stopped." ;;
        restart_done) echo "Stack restarted." ;;
        enter_service_name) echo -n "Service name (or Enter for all): " ;;
        press_enter) echo -n "Press Enter to continue..." ;;
        env_created) echo ".env created from .env.example. Fill values before production use." ;;
        env_missing) echo "Neither .env nor .env.example found. Create .env manually." ;;
        autostart_done) echo "systemd autostart configured." ;;
        only_linux_systemd) echo "systemd setup is supported on Linux only." ;;
        prereq_title) echo "Checking and installing dependencies..." ;;
        done) echo "Done." ;;
        fail) echo "Failed." ;;
        current_dir_not_repo) echo "Current directory does not look like app directory." ;;
        *)
          echo "$key"
          ;;
      esac
      ;;
  esac
}

pause_screen() {
  printf '\n'
  msg press_enter
  read -r _
}

choose_language() {
  while true; do
    echo "$(msg choose_lang)"
    echo "$(msg choose_lang_hint)"
    read -r lang_pick
    case "$lang_pick" in
      1) LANG_CHOICE="ru"; break ;;
      2) LANG_CHOICE="en"; break ;;
      ru|RU|russian|русский) LANG_CHOICE="ru"; break ;;
      en|EN|english) LANG_CHOICE="en"; break ;;
      *) echo "$(msg invalid_choice)" ;;
    esac
  done
}

is_installed() {
  [[ -f "$APP_DIR/$DOCKER_COMPOSE_FILE" ]]
}

require_app_installed() {
  if ! is_installed; then
    echo "$(msg need_install_first)"
    return 1
  fi
}

run_compose() {
  (cd "$APP_DIR" && docker compose "$@")
}

ensure_app_dir_writable() {
  if [[ ! -w "$APP_DIR" ]]; then
    if [[ -n "$SUDO" ]]; then
      $SUDO chown -R "$(id -un)":"$(id -gn)" "$APP_DIR"
    fi
  fi
}

ensure_linux() {
  [[ "$(uname -s)" == "Linux" ]]
}

install_docker_ubuntu() {
  $SUDO apt-get update
  $SUDO apt-get install -y ca-certificates curl gnupg
  $SUDO install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.asc
    $SUDO chmod a+r /etc/apt/keyrings/docker.asc
  fi
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
  $SUDO apt-get update
  $SUDO apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin git
  $SUDO systemctl enable docker
  $SUDO systemctl start docker
}

ensure_prerequisites() {
  echo "$(msg prereq_title)"
  if ! ensure_linux; then
    log "This script is optimized for Linux servers."
  fi

  if ! command -v git >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      $SUDO apt-get update && $SUDO apt-get install -y git
    else
      log "Install git manually and re-run."
      return 1
    fi
  fi

  if ! command -v docker >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      install_docker_ubuntu
    else
      log "Install Docker manually and re-run."
      return 1
    fi
  fi

  if ! docker compose version >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      $SUDO apt-get update && $SUDO apt-get install -y docker-compose-plugin
    else
      log "Install docker compose plugin manually and re-run."
      return 1
    fi
  fi

  $SUDO systemctl enable docker >/dev/null 2>&1 || true
  $SUDO systemctl start docker >/dev/null 2>&1 || true
}

prompt_install_vars() {
  printf "%s " "$(msg app_dir_prompt)"
  read -r input_dir || true
  if [[ -n "${input_dir:-}" ]]; then
    APP_DIR="$input_dir"
  fi

  printf "%s " "$(msg repo_prompt)"
  read -r input_repo || true
  if [[ -n "${input_repo:-}" ]]; then
    REPO_URL="$input_repo"
  fi
}

first_install() {
  echo "$(msg install_started)"
  ensure_prerequisites
  prompt_install_vars

  $SUDO mkdir -p "$APP_DIR"
  ensure_app_dir_writable
  if [[ -z "$(ls -A "$APP_DIR" 2>/dev/null || true)" ]]; then
    git clone "$REPO_URL" "$APP_DIR"
  else
    if [[ -d "$APP_DIR/.git" ]]; then
      (cd "$APP_DIR" && git fetch --all --prune && git pull --ff-only)
    else
      echo "$(msg current_dir_not_repo)"
      return 1
    fi
  fi

  if [[ ! -f "$APP_DIR/.env" ]]; then
    if [[ -f "$APP_DIR/.env.example" ]]; then
      $SUDO cp "$APP_DIR/.env.example" "$APP_DIR/.env"
      echo "$(msg env_created)"
    else
      echo "$(msg env_missing)"
    fi
  fi

  run_compose down --remove-orphans || true
  run_compose up -d --build --force-recreate
  if [[ -f "$APP_DIR/scripts/docker-stack-start.sh" ]]; then
    chmod +x "$APP_DIR/scripts/docker-stack-start.sh" 2>/dev/null || $SUDO chmod +x "$APP_DIR/scripts/docker-stack-start.sh"
  fi
  echo "$(msg install_done)"
}

check_updates() {
  require_app_installed || return 1
  echo "$(msg checking_updates)"
  (
    cd "$APP_DIR"
    if [[ ! -d .git ]]; then
      log "Not a git repository in $APP_DIR."
      return 2
    fi
    git fetch --quiet origin || true
    local_branch="$(git rev-parse @)"
    remote_branch="$(git rev-parse @{u} 2>/dev/null || true)"
    if [[ -z "$remote_branch" ]]; then
      return 2
    fi
    if [[ "$local_branch" != "$remote_branch" ]]; then
      return 10
    fi
    return 0
  )
  code=$?
  if [[ $code -eq 10 ]]; then
    echo "$(msg updates_found)"
    return 10
  fi
  if [[ $code -eq 0 ]]; then
    echo "$(msg updates_not_found)"
    return 0
  fi
  log "Update check skipped (no tracking branch or fetch issue)."
  return 1
}

update_system() {
  require_app_installed || return 1
  echo "$(msg update_started)"
  ensure_prerequisites
  (
    cd "$APP_DIR"
    if [[ -d .git ]]; then
      git fetch --all --prune
      git pull --ff-only || true
    fi
  )
  run_compose up -d --build --remove-orphans
  echo "$(msg update_done)"
}

start_stack() {
  require_app_installed || return 1
  if [[ -x "$APP_DIR/scripts/docker-stack-start.sh" ]]; then
    bash "$APP_DIR/scripts/docker-stack-start.sh"
  else
    run_compose up -d
  fi
  echo "$(msg start_done)"
}

stop_stack() {
  require_app_installed || return 1
  run_compose down
  echo "$(msg stop_done)"
}

restart_stack() {
  require_app_installed || return 1
  if [[ -x "$APP_DIR/scripts/docker-stack-start.sh" ]]; then
    bash "$APP_DIR/scripts/docker-stack-start.sh"
  else
    run_compose down
    run_compose up -d
  fi
  echo "$(msg restart_done)"
}

show_status() {
  require_app_installed || return 1
  run_compose ps
}

show_logs() {
  require_app_installed || return 1
  msg enter_service_name
  read -r svc
  if [[ -n "${svc:-}" ]]; then
    run_compose logs -f --tail=200 "$svc"
  else
    run_compose logs -f --tail=200
  fi
}

rebuild_stack() {
  require_app_installed || return 1
  run_compose up -d --build --force-recreate
}

run_migrations() {
  require_app_installed || return 1
  run_compose run --rm migrate
}

fix_docker_network() {
  require_app_installed || return 1
  if [[ -x "$APP_DIR/scripts/docker-stack-start.sh" ]]; then
    bash "$APP_DIR/scripts/docker-stack-start.sh"
  else
    run_compose down --remove-orphans || true
    docker rm -f "${APP_NAME}-postgres-1" >/dev/null 2>&1 || true
    run_compose up -d --force-recreate
  fi
  run_compose ps
}

setup_systemd_autostart() {
  require_app_installed || return 1
  if ! ensure_linux; then
    echo "$(msg only_linux_systemd)"
    return 1
  fi

  local starter="$APP_DIR/scripts/docker-stack-start.sh"
  if [[ ! -f "$starter" ]]; then
    log "Не найден $starter — обновите репозиторий."
    return 1
  fi
  chmod +x "$starter" 2>/dev/null || $SUDO chmod +x "$starter"

  local flux="$APP_DIR/scripts/flux_init.sh"
  if [[ -f "$flux" ]]; then
    chmod +x "$flux" 2>/dev/null || $SUDO chmod +x "$flux"
  fi
  if [[ -f "$APP_DIR/flux_init" ]]; then
    chmod +x "$APP_DIR/flux_init" 2>/dev/null || $SUDO chmod +x "$APP_DIR/flux_init"
  fi

  local unit_path="/etc/systemd/system/$SERVICE_NAME"
  $SUDO tee "$unit_path" >/dev/null <<EOF
[Unit]
Description=Remna Bot stack (Docker Compose) — boot via docker-stack-start.sh
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
# Неинтерактивный подъём после reboot (все сервисы, включая idbot).
ExecStart=$starter
# Остановка стека при остановке unit (опционально).
ExecStop=-/usr/bin/docker compose down --remove-orphans
RemainAfterExit=yes
TimeoutStartSec=0
# Интерактивное меню на сервере (запуск/стоп/логи/systemd): ./scripts/flux_init.sh или ./flux_init

[Install]
WantedBy=multi-user.target
EOF

  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$SERVICE_NAME"
  $SUDO systemctl restart "$SERVICE_NAME"
  echo "$(msg autostart_done)"
}

diagnostics() {
  echo "===== Diagnostics ====="
  echo "OS: $(uname -a)"
  echo "APP_DIR: $APP_DIR"
  echo "SERVICE_NAME: $SERVICE_NAME"
  echo "REPO_URL: $REPO_URL"
  command -v git >/dev/null 2>&1 && echo "git: $(git --version)" || echo "git: not found"
  command -v docker >/dev/null 2>&1 && echo "docker: $(docker --version)" || echo "docker: not found"
  docker compose version >/dev/null 2>&1 && echo "compose: $(docker compose version)" || echo "compose: not found"
  if is_installed; then
    run_compose ps || true
  fi
}

preflight_auto() {
  if is_installed; then
    echo "$(msg installed_yes)"
    if check_updates; then
      true
    else
      rc=$?
      if [[ $rc -eq 10 ]]; then
        printf "%s " "$(msg offer_update)"
        read -r answer
        if [[ "$answer" =~ ^[YyАаДд]$ ]]; then
          update_system
        fi
      fi
    fi
  else
    echo "$(msg installed_no)"
    printf "%s " "$(msg offer_install)"
    read -r answer
    if [[ "$answer" =~ ^[YyАаДд]$ ]]; then
      first_install
    fi
  fi
}

menu_loop() {
  while true; do
    echo
    echo "$(msg menu_title)"
    echo "$(msg menu_install)"
    echo "$(msg menu_update)"
    echo "$(msg menu_start)"
    echo "$(msg menu_stop)"
    echo "$(msg menu_restart)"
    echo "$(msg menu_status)"
    echo "$(msg menu_logs)"
    echo "$(msg menu_rebuild)"
    echo "$(msg menu_migrate)"
    echo "$(msg menu_fix_net)"
    echo "$(msg menu_autostart)"
    echo "$(msg menu_diag)"
    echo "$(msg menu_lang)"
    echo "$(msg menu_exit)"
    msg enter_choice
    read -r choice
    case "$choice" in
      1) first_install; pause_screen ;;
      2) update_system; pause_screen ;;
      3) start_stack; pause_screen ;;
      4) stop_stack; pause_screen ;;
      5) restart_stack; pause_screen ;;
      6) show_status; pause_screen ;;
      7) show_logs ;;
      8) rebuild_stack; pause_screen ;;
      9) run_migrations; pause_screen ;;
      10) fix_docker_network; pause_screen ;;
      11) setup_systemd_autostart; pause_screen ;;
      12) diagnostics; pause_screen ;;
      13) choose_language ;;
      0) exit 0 ;;
      *) echo "$(msg invalid_choice)" ;;
    esac
  done
}

main() {
  choose_language
  preflight_auto
  menu_loop
}

# Быстрый вход из flux_init: сразу меню без выбора языка и без preflight.
# Задаётся APP_DIR из окружения (scripts/flux_init.sh экспортирует каталог репозитория).
if [[ "${1:-}" == "flux-menu" ]]; then
  shift || true
  APP_DIR="${APP_DIR:-$DEFAULT_APP_DIR}"
  if ! [[ -f "$APP_DIR/$DOCKER_COMPOSE_FILE" ]]; then
    echo "flux-menu: не найден $APP_DIR/$DOCKER_COMPOSE_FILE — задайте APP_DIR=/path/to/remna-bot" >&2
    exit 1
  fi
  LANG_CHOICE="${FLUX_LANG:-ru}"
  menu_loop
  exit 0
fi

main "$@"
