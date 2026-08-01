#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${CLIENTPLATFORM_ROOT:-/opt/clientplatform}"
ENV_FILE="$ROOT/deploy/clientplatform/clientplatform.env"
SQUID_CONFIG="/etc/squid/squid.conf"
LOCAL_RELAY_SERVICE="/etc/systemd/system/clientplatform-telegram-ipv6-relay.service"
LOCAL_RELAY_DIR="/opt/clientplatform-telegram-ipv6-relay"
APP="clientplatform-production-app-1"

fail() {
    printf 'CLIENTPLATFORM_TELEGRAM_RELAY_CLEANUP_FAILED:%s\n' "$1" >&2
    exit 1
}

if [ "$(id -u)" -ne 0 ]; then
    fail "root_required"
fi

if [ -f "$SQUID_CONFIG" ] \
    && grep -Fq 'visible_hostname clientplatform-telegram-relay' "$SQUID_CONFIG"; then
    systemctl disable --now squid.service >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get purge -y \
        squid squid-common squid-langpack >/dev/null 2>&1 || true
    rm -rf /etc/squid /var/spool/squid
    printf 'ACCIDENTAL_CLIENTPLATFORM_SQUID_REMOVED\n'
elif systemctl is-active --quiet squid.service 2>/dev/null; then
    fail "unrelated_active_squid_detected"
fi

if [ -f "$LOCAL_RELAY_SERVICE" ]; then
    if ! grep -Fq 'ExecStart=/usr/bin/python3 /opt/clientplatform-telegram-ipv6-relay/relay.py' \
        "$LOCAL_RELAY_SERVICE"; then
        fail "unrelated_local_relay_service_detected"
    fi
    systemctl disable --now clientplatform-telegram-ipv6-relay.service \
        >/dev/null 2>&1 || true
    rm -f "$LOCAL_RELAY_SERVICE"
    systemctl daemon-reload
    systemctl reset-failed clientplatform-telegram-ipv6-relay.service \
        >/dev/null 2>&1 || true
    printf 'ACCIDENTAL_LOCAL_IPV6_RELAY_REMOVED\n'
fi
rm -rf "$LOCAL_RELAY_DIR"

if [ -f "$ENV_FILE" ]; then
    [ ! -L "$ENV_FILE" ] || fail "production_env_must_not_be_symlink"
    python3 - "$ENV_FILE" <<'PY'
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

path = Path(sys.argv[1])
known_hosts = {
    "147.45.146.112",
    "127.0.0.1",
    "localhost",
    "host.docker.internal",
}
original = path.read_text(encoding="utf-8")
result: list[str] = []
removed = False
unexpected_proxy = ""

for raw in original.splitlines():
    stripped = raw.strip()
    if not stripped.startswith("TELEGRAM_PROXY_URL="):
        result.append(raw)
        continue

    value = stripped.split("=", 1)[1].strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        parsed = None

    host = (parsed.hostname or "").lower() if parsed is not None else ""
    if host in known_hosts:
        removed = True
        continue

    unexpected_proxy = value
    result.append(raw)

if unexpected_proxy:
    print("UNRELATED_TELEGRAM_PROXY_PRESERVED")

payload = "\n".join(result).rstrip() + "\n"
if removed and payload != original:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.before-failed-relay-cleanup-{stamp}")
    backup.write_bytes(path.read_bytes())
    os.chmod(backup, 0o600)

    temporary = path.with_name(f".{path.name}.relay-cleanup-{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    print("ACCIDENTAL_TELEGRAM_PROXY_ENV_REMOVED")

os.chmod(path, 0o600)
PY
fi

if ss -ltn 2>/dev/null | grep -Eq ':3128([[:space:]]|$)'; then
    fail "port_3128_still_listening"
fi

if docker inspect "$APP" >/dev/null 2>&1; then
    APP_RUNNING="$(docker inspect "$APP" --format '{{.State.Running}}')"
    APP_RESTARTS="$(docker inspect "$APP" --format '{{.RestartCount}}')"
    printf 'production_app_running=%s restart_count=%s\n' "$APP_RUNNING" "$APP_RESTARTS"
fi

printf 'CLIENTPLATFORM_FAILED_TELEGRAM_RELAYS_CLEANED\n'
