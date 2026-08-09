#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${CLIENTPLATFORM_ROOT:-/opt/clientplatform}"
ENV_FILE="$ROOT/deploy/clientplatform/clientplatform.env"
SQUID_CONFIG="/etc/squid/squid.conf"
SQUID_BACKUP_GLOB="/etc/squid/squid.conf.before-clientplatform-*"
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
    # The historical installer copied any configuration it found to
    # squid.conf.before-clientplatform-<UTC stamp> before replacing it. If such
    # provenance exists, fail safe: restore the newest saved configuration and
    # retain the Squid package instead of purging a potentially unrelated proxy.
    SQUID_BACKUP="$(
        find /etc/squid -maxdepth 1 -type f \
            -name 'squid.conf.before-clientplatform-*' \
            -printf '%T@ %p\n' 2>/dev/null \
            | sort -nr \
            | head -n 1 \
            | cut -d' ' -f2-
    )"
    systemctl stop squid.service >/dev/null 2>&1 || true
    if [ -n "$SQUID_BACKUP" ] && [ -f "$SQUID_BACKUP" ]; then
        cp --preserve=mode,ownership,timestamps "$SQUID_BACKUP" "$SQUID_CONFIG"
        if command -v squid >/dev/null 2>&1; then
            squid -k parse >/dev/null 2>&1 \
                || fail "restored_squid_config_invalid"
            systemctl start squid.service >/dev/null 2>&1 \
                || fail "restored_squid_start_failed"
        fi
        printf 'PRE_CLIENTPLATFORM_SQUID_CONFIG_RESTORED:%s\n' "$SQUID_BACKUP"
    else
        DEBIAN_FRONTEND=noninteractive apt-get purge -y \
            squid squid-common squid-langpack >/dev/null 2>&1 || true
        rm -rf /etc/squid /var/spool/squid
        printf 'ACCIDENTAL_CLIENTPLATFORM_SQUID_REMOVED\n'
    fi
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

# These are the exact endpoint shapes created by the failed relay experiments.
# Matching host alone is unsafe: production may intentionally use another
# proxy on the same machine and that configuration must be preserved.
known_failed_endpoints = {
    ("http", "147.45.146.112", 3128),
    ("http", "127.0.0.1", 3128),
    ("http", "localhost", 3128),
    ("http", "host.docker.internal", 3128),
}


def normalized_endpoint(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "http"
        or not parsed.hostname
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return (parsed.scheme.lower(), parsed.hostname.lower(), port)


original = path.read_text(encoding="utf-8")
result: list[str] = []
removed = False
preserved_proxy = False

for raw in original.splitlines():
    stripped = raw.strip()
    if not stripped.startswith("TELEGRAM_PROXY_URL="):
        result.append(raw)
        continue

    value = stripped.split("=", 1)[1].strip()
    if normalized_endpoint(value) in known_failed_endpoints:
        removed = True
        continue

    preserved_proxy = True
    result.append(raw)

if preserved_proxy:
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

# Do not require global port 3128 to become unused: a restored/unrelated Squid
# or another operator-managed proxy may legitimately own it. Ownership is
# established by the exact ClientPlatform config/unit markers above.
if ss -ltn 2>/dev/null | grep -Eq ':3128([[:space:]]|$)'; then
    printf 'UNRELATED_OR_RESTORED_PORT_3128_LISTENER_PRESERVED\n'
fi

if docker inspect "$APP" >/dev/null 2>&1; then
    APP_RUNNING="$(docker inspect "$APP" --format '{{.State.Running}}')"
    APP_RESTARTS="$(docker inspect "$APP" --format '{{.RestartCount}}')"
    printf 'production_app_running=%s restart_count=%s\n' "$APP_RUNNING" "$APP_RESTARTS"
fi

printf 'CLIENTPLATFORM_FAILED_TELEGRAM_RELAYS_CLEANED\n'
