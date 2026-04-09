#!/usr/bin/env bash
# Опционально запускает процесс бота через proxychains4 (весь исходящий TCP — через SOCKS5, например локальный VPN).
# Включение: BOT_PROXYCHAINS_ENABLED=true в .env (см. PROXYCHAINS_*).
set -euo pipefail

if [[ "${BOT_PROXYCHAINS_ENABLED:-}" == "true" || "${BOT_PROXYCHAINS_ENABLED:-}" == "1" ]]; then
  HOST="${PROXYCHAINS_SOCKS5_HOST:-127.0.0.1}"
  PORT="${PROXYCHAINS_SOCKS5_PORT:-1080}"
  # proxychains4 в [ProxyList] требует числовой IPv4; имён (host.docker.internal) не принимает.
  if [[ ! "${HOST}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    HOST="$(getent hosts "${HOST}" | awk '$1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ { print $1; exit }')"
    if [[ -z "${HOST}" ]]; then
      echo "bot-entrypoint: не удалось разрешить PROXYCHAINS_SOCKS5_HOST в IPv4 (нужен для proxychains4)" >&2
      exit 1
    fi
  fi
  # proxy_dns по умолчанию выкл.: иначе DNS идёт через SOCKS и ломается резолв имён Docker (postgres/redis).
  # Включить только если нужен DNS через прокси: PROXYCHAINS_PROXY_DNS=true
  PROXY_DNS_BLOCK=""
  if [[ "${PROXYCHAINS_PROXY_DNS:-}" == "true" || "${PROXYCHAINS_PROXY_DNS:-}" == "1" ]]; then
    PROXY_DNS_BLOCK="proxy_dns
remote_dns_subnet 224"
  fi
  cat >/etc/proxychains4.conf <<EOF
# Сгенерировано scripts/bot-entrypoint.sh (не править вручную в контейнере)
strict_chain
${PROXY_DNS_BLOCK}
tcp_read_time_out 15000
tcp_connect_time_out 8000
# Не проксировать локальные/докер-сети: иначе postgres/redis в compose не достучаться.
localnet 127.0.0.0/255.0.0.0
localnet 10.0.0.0/255.0.0.0
localnet 172.16.0.0/255.240.0.0
localnet 192.168.0.0/255.255.0.0

[ProxyList]
socks5 ${HOST} ${PORT}
EOF
  exec proxychains4 -q "$@"
else
  exec "$@"
fi
