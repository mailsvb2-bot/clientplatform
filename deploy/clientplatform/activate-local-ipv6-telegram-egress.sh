#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${CLIENTPLATFORM_ROOT:-/opt/clientplatform}"
EXPECTED_SHA="${CLIENTPLATFORM_EXPECTED_SHA:-}"
EXPECTED_IPV4="${CLIENTPLATFORM_EXPECTED_IPV4:-185.104.114.163}"
TARGET_REF="${CLIENTPLATFORM_TARGET_REF:-main}"
RELAY_PORT="${TELEGRAM_IPV6_RELAY_PORT:-3128}"
ENV_FILE="$ROOT/deploy/clientplatform/clientplatform.env"
RELAY_SOURCE="$ROOT/deploy/clientplatform/telegram_ipv6_connect_relay.py"
RELAY_DIR="/opt/clientplatform-telegram-ipv6-relay"
RELAY_TARGET="$RELAY_DIR/relay.py"
SERVICE_FILE="/etc/systemd/system/clientplatform-telegram-ipv6-relay.service"
APP="clientplatform-production-app-1"

fail() {
    printf 'CLIENTPLATFORM_LOCAL_IPV6_EGRESS_FAILED:%s\n' "$1" >&2
    exit 1
}

if [ "$(id -u)" -ne 0 ]; then
    fail "root_required"
fi

python3 - "$EXPECTED_SHA" "$EXPECTED_IPV4" "$RELAY_PORT" <<'PY'
from __future__ import annotations

import ipaddress
import re
import sys

sha, ipv4_text, port_text = sys.argv[1:]
if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
    raise SystemExit("CLIENTPLATFORM_LOCAL_IPV6_EGRESS_FAILED:invalid_expected_sha")
address = ipaddress.ip_address(ipv4_text)
if address.version != 4 or address.is_unspecified or address.is_multicast:
    raise SystemExit("CLIENTPLATFORM_LOCAL_IPV6_EGRESS_FAILED:invalid_expected_ipv4")
port = int(port_text)
if not 1024 <= port <= 65535:
    raise SystemExit("CLIENTPLATFORM_LOCAL_IPV6_EGRESS_FAILED:invalid_relay_port")
PY

CURRENT_IPV4="$(
    ip -4 route get 1.1.1.1 \
        | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}'
)"

printf 'clientplatform_ipv4=%s expected_ipv4=%s\n' "$CURRENT_IPV4" "$EXPECTED_IPV4"
[ "$CURRENT_IPV4" = "$EXPECTED_IPV4" ] || fail "wrong_server"

TELEGRAM_IPV6="$(
    getent ahostsv6 api.telegram.org \
        | awk '$2 == "STREAM" {print $1; exit}'
)"
[ -n "$TELEGRAM_IPV6" ] || fail "telegram_aaaa_missing"

HOST_IPV6="$(
    ip -6 route get "$TELEGRAM_IPV6" \
        | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}'
)"
[ -n "$HOST_IPV6" ] || fail "host_ipv6_route_missing"

printf 'telegram_ipv6=%s host_source_ipv6=%s\n' "$TELEGRAM_IPV6" "$HOST_IPV6"

DIRECT_IPV6_HTTP="$(
    curl -6 \
        --noproxy '*' \
        --silent \
        --show-error \
        --connect-timeout 12 \
        --max-time 30 \
        --output /dev/null \
        --write-out '%{http_code}' \
        https://api.telegram.org/ \
        || true
)"
printf 'direct_telegram_ipv6_http=%s\n' "${DIRECT_IPV6_HTTP:-000}"
case "$DIRECT_IPV6_HTTP" in
    200|301|302|404) ;;
    *) fail "direct_telegram_ipv6_unavailable" ;;
esac

cd "$ROOT"
if [ ! -d .git ]; then
    fail "repository_missing"
fi

git fetch --no-tags --prune --depth 1 origin "$TARGET_REF"
TARGET_SHA="$(git rev-parse FETCH_HEAD)"
[ "$TARGET_SHA" = "$EXPECTED_SHA" ] || fail "unexpected_target_sha"
git reset --hard "$TARGET_SHA"

[ -f "$ENV_FILE" ] || fail "production_env_missing"
[ ! -L "$ENV_FILE" ] || fail "production_env_symlink"
[ -f "$RELAY_SOURCE" ] || fail "relay_source_missing"

if systemctl list-unit-files squid.service >/dev/null 2>&1; then
    if [ -f /etc/squid/squid.conf ] \
        && grep -Fq 'visible_hostname clientplatform-telegram-relay' /etc/squid/squid.conf; then
        systemctl disable --now squid >/dev/null 2>&1 || true
        DEBIAN_FRONTEND=noninteractive apt-get purge -y \
            squid squid-common squid-langpack >/dev/null 2>&1 || true
        rm -rf /etc/squid /var/spool/squid
        printf 'ACCIDENTAL_SQUID_REMOVED\n'
    elif systemctl is-active --quiet squid; then
        fail "unrelated_squid_conflict"
    fi
fi

install -d -m 0755 "$RELAY_DIR"
install -m 0755 "$RELAY_SOURCE" "$RELAY_TARGET"

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=ClientPlatform restricted Telegram IPv6 CONNECT relay
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${RELAY_TARGET} --listen-host 0.0.0.0 --listen-port ${RELAY_PORT}
Restart=always
RestartSec=2
DynamicUser=yes
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictAddressFamilies=AF_INET AF_INET6
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
EOF

chmod 0644 "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable --now clientplatform-telegram-ipv6-relay.service >/dev/null

for _ in $(seq 1 30); do
    if systemctl is-active --quiet clientplatform-telegram-ipv6-relay.service \
        && ss -ltn | grep -Eq ":${RELAY_PORT}([[:space:]]|$)"; then
        break
    fi
    sleep 1
done

systemctl is-active --quiet clientplatform-telegram-ipv6-relay.service \
    || fail "relay_service_not_active"
ss -ltn | grep -Eq ":${RELAY_PORT}([[:space:]]|$)" \
    || fail "relay_listener_missing"

PROXY_HTTP="$(
    curl -4 \
        --noproxy '' \
        --proxy "http://127.0.0.1:${RELAY_PORT}" \
        --silent \
        --show-error \
        --connect-timeout 12 \
        --max-time 30 \
        --output /dev/null \
        --write-out '%{http_code}' \
        https://api.telegram.org/ \
        || true
)"
printf 'telegram_via_local_ipv6_relay_http=%s\n' "${PROXY_HTTP:-000}"
case "$PROXY_HTTP" in
    200|301|302|404) ;;
    *)
        journalctl -u clientplatform-telegram-ipv6-relay.service \
            --since '-10 minutes' --no-pager | tail -n 120 >&2 || true
        fail "local_ipv6_relay_probe_failed"
        ;;
esac

python3 - "$ENV_FILE" "$RELAY_PORT" <<'PY'
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
port = int(sys.argv[2])
updates = {
    "TELEGRAM_TRANSPORT": "polling",
    "TELEGRAM_WEBHOOK_ENABLED": "0",
    "TELEGRAM_IP_FAMILY": "ipv4",
    "TELEGRAM_FORCE_CLOSE": "1",
    "TELEGRAM_DNS_TTL_SEC": "60",
    "TELEGRAM_REQUEST_TIMEOUT_SEC": "25",
    "TELEGRAM_NETWORK_RETRIES": "5",
    "TELEGRAM_NETWORK_RETRY_DELAY_SEC": "1",
    "TELEGRAM_NETWORK_RETRY_MAX_DELAY_SEC": "8",
    "TELEGRAM_PROXY_URL": f"http://host.docker.internal:{port}",
}
original = path.read_text(encoding="utf-8")
result: list[str] = []
seen: set[str] = set()
for raw in original.splitlines():
    stripped = raw.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        result.append(raw)
        continue
    key = stripped.split("=", 1)[0].strip()
    if key in updates:
        result.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        result.append(raw)
for key, value in updates.items():
    if key not in seen:
        result.append(f"{key}={value}")
payload = "\n".join(result).rstrip() + "\n"
if payload != original:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.before-local-ipv6-egress-{stamp}")
    backup.write_bytes(path.read_bytes())
    os.chmod(backup, 0o600)
    temporary = path.with_name(f".{path.name}.local-ipv6-{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
os.chmod(path, 0o600)
print("CLIENTPLATFORM_LOCAL_IPV6_EGRESS_ENV_OK")
PY

BACKUP_ARGS=()
if ! grep -Eq '^CLIENTPLATFORM_BACKUP_AGE_RECIPIENT=age1[^[:space:]]+' "$ENV_FILE"; then
    BACKUP_ARGS=(--allow-local-backup)
fi

python3 -m scripts.clientplatform_production_deploy \
    --recover-unavailable-baseline \
    --timeout-seconds 420 \
    "${BACKUP_ARGS[@]}"

[ "$(git rev-parse HEAD)" = "$EXPECTED_SHA" ] || fail "deployed_sha_mismatch"
[ "$(docker inspect "$APP" --format '{{.State.Running}}')" = "true" ] \
    || fail "app_not_running"

docker exec -i "$APP" python - <<'PY'
from __future__ import annotations

import asyncio
import os

from core.telegram_bot import PollingAiohttpSession, build_bot


async def main() -> None:
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("CLIENTPLATFORM_LOCAL_IPV6_EGRESS_PROOF_FAILED:token_missing")
    bot = build_bot(token)
    try:
        if not isinstance(bot.session, PollingAiohttpSession):
            raise SystemExit(
                "CLIENTPLATFORM_LOCAL_IPV6_EGRESS_PROOF_FAILED:unexpected_session"
            )
        if bot.session.proxy_mode != "http_connect":
            raise SystemExit(
                "CLIENTPLATFORM_LOCAL_IPV6_EGRESS_PROOF_FAILED:proxy_not_active"
            )
        me = await bot.get_me()
        webhook = await bot.get_webhook_info()
        if not me.is_bot:
            raise SystemExit(
                "CLIENTPLATFORM_LOCAL_IPV6_EGRESS_PROOF_FAILED:not_a_bot"
            )
        if webhook.url:
            raise SystemExit(
                "CLIENTPLATFORM_LOCAL_IPV6_EGRESS_PROOF_FAILED:webhook_enabled"
            )
        print(
            "CLIENTPLATFORM_LOCAL_IPV6_EGRESS_GETME_OK:"
            f"id={me.id} username={me.username or 'unknown'} mode=polling"
        )
    finally:
        await bot.session.close()


asyncio.run(main())
PY

STARTED_AT="$(docker inspect "$APP" --format '{{.State.StartedAt}}')"
for _ in $(seq 1 30); do
    if docker logs --since "$STARTED_AT" "$APP" 2>&1 \
        | grep -Fq 'Run polling for bot @clientplatform_bot'; then
        printf 'CLIENTPLATFORM_TELEGRAM_POLLING_STARTED\n'
        printf 'CLIENTPLATFORM_LOCAL_IPV6_EGRESS_OK:sha=%s\n' "$EXPECTED_SHA"
        exit 0
    fi
    sleep 3
done

fail "polling_start_marker_missing"
