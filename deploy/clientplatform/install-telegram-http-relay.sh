#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_IP="${CLIENTPLATFORM_SOURCE_IP:-}"
RELAY_PORT="${TELEGRAM_RELAY_PORT:-3128}"
CONFIG_PATH="/etc/squid/squid.conf"

fail() {
    printf 'CLIENTPLATFORM_TELEGRAM_RELAY_FAILED:%s\n' "$1" >&2
    exit 1
}

if [ "$(id -u)" -ne 0 ]; then
    fail "root_required"
fi

python3 - "$SOURCE_IP" "$RELAY_PORT" <<'PY'
from __future__ import annotations

import ipaddress
import sys

source_ip, port_text = sys.argv[1:]

try:
    address = ipaddress.ip_address(source_ip)
except ValueError as exc:
    raise SystemExit("CLIENTPLATFORM_TELEGRAM_RELAY_FAILED:invalid_source_ip") from exc

if address.version != 4 or address.is_unspecified or address.is_multicast:
    raise SystemExit("CLIENTPLATFORM_TELEGRAM_RELAY_FAILED:invalid_source_ip")

try:
    port = int(port_text)
except ValueError as exc:
    raise SystemExit("CLIENTPLATFORM_TELEGRAM_RELAY_FAILED:invalid_port") from exc

if not 1024 <= port <= 65535:
    raise SystemExit("CLIENTPLATFORM_TELEGRAM_RELAY_FAILED:invalid_port")
PY

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl squid

install -d -m 0750 /etc/squid

if [ -f "$CONFIG_PATH" ]; then
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    cp --preserve=mode,ownership,timestamps "$CONFIG_PATH" "${CONFIG_PATH}.before-clientplatform-${stamp}"
fi

cat >"$CONFIG_PATH" <<EOF
visible_hostname clientplatform-telegram-relay

# Listen on IPv4. Access is fail-closed by the source and destination ACLs below.
http_port 0.0.0.0:${RELAY_PORT}

acl clientplatform_source src ${SOURCE_IP}/32
acl local_relay_test src 127.0.0.1/32
acl telegram_api dstdomain api.telegram.org
acl telegram_tls_port port 443
acl CONNECT method CONNECT

# Only ClientPlatform (and a localhost health probe) may open a TLS tunnel,
# and the only permitted destination is api.telegram.org:443.
http_access allow clientplatform_source telegram_api telegram_tls_port CONNECT
http_access allow local_relay_test telegram_api telegram_tls_port CONNECT
http_access deny all

cache deny all
via off
forwarded_for delete
shutdown_lifetime 1 seconds

access_log stdio:/var/log/squid/access.log squid
cache_log /var/log/squid/cache.log
coredump_dir /var/spool/squid
EOF

chmod 0640 "$CONFIG_PATH"
chown root:proxy "$CONFIG_PATH" 2>/dev/null || chown root:squid "$CONFIG_PATH" 2>/dev/null || true

/usr/sbin/squid -k parse
systemctl enable squid >/dev/null
systemctl restart squid

for _ in $(seq 1 30); do
    if systemctl is-active --quiet squid; then
        break
    fi
    sleep 1
done

systemctl is-active --quiet squid || fail "service_not_active"

HTTP_CODE="$(
    curl -4 \
        --proxy "http://127.0.0.1:${RELAY_PORT}" \
        --silent \
        --show-error \
        --connect-timeout 10 \
        --max-time 25 \
        --output /dev/null \
        --write-out '%{http_code}' \
        https://api.telegram.org/ \
        || true
)"

case "$HTTP_CODE" in
    200|301|302|404)
        ;;
    *)
        journalctl -u squid --since '-10 minutes' --no-pager | tail -n 100 >&2 || true
        fail "telegram_probe_http_${HTTP_CODE:-000}"
        ;;
esac

ss -ltnp | grep -E "LISTEN.+:${RELAY_PORT}([[:space:]]|$)" || fail "listen_socket_missing"

printf 'CLIENTPLATFORM_TELEGRAM_RELAY_OK:source=%s port=%s telegram_http=%s\n' \
    "$SOURCE_IP" "$RELAY_PORT" "$HTTP_CODE"
