#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${CLIENTPLATFORM_ROOT:-/opt/clientplatform}"
RELAY_URL="${TELEGRAM_RELAY_URL:-}"
EXPECTED_SHA="${CLIENTPLATFORM_EXPECTED_SHA:-}"
ENV_FILE="$ROOT/deploy/clientplatform/clientplatform.env"
APP="clientplatform-production-app-1"

fail() {
    printf 'CLIENTPLATFORM_TELEGRAM_RELAY_ACTIVATION_FAILED:%s\n' "$1" >&2
    exit 1
}

if [ "$(id -u)" -ne 0 ]; then
    fail "root_required"
fi

python3 - "$RELAY_URL" "$EXPECTED_SHA" <<'PY'
from __future__ import annotations

import re
import sys
from urllib.parse import urlsplit

relay_url, expected_sha = sys.argv[1:]

try:
    parsed = urlsplit(relay_url)
    port = parsed.port
except ValueError as exc:
    raise SystemExit(
        "CLIENTPLATFORM_TELEGRAM_RELAY_ACTIVATION_FAILED:invalid_relay_url"
    ) from exc

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
    raise SystemExit(
        "CLIENTPLATFORM_TELEGRAM_RELAY_ACTIVATION_FAILED:invalid_relay_url"
    )

if re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None:
    raise SystemExit(
        "CLIENTPLATFORM_TELEGRAM_RELAY_ACTIVATION_FAILED:invalid_expected_sha"
    )
PY

[ -f "$ENV_FILE" ] || fail "production_env_missing"
[ ! -L "$ENV_FILE" ] || fail "production_env_must_not_be_symlink"
chmod 0600 "$ENV_FILE"

HTTP_CODE="$(
    curl -4 \
        --proxy "$RELAY_URL" \
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
        fail "relay_probe_http_${HTTP_CODE:-000}"
        ;;
esac

python3 - "$ENV_FILE" "$RELAY_URL" <<'PY'
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
relay_url = sys.argv[2]

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
    "TELEGRAM_PROXY_URL": relay_url,
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
    backup = path.with_name(f"{path.name}.before-telegram-relay-{stamp}")
    backup.write_bytes(path.read_bytes())
    os.chmod(backup, 0o600)

    temporary = path.with_name(f".{path.name}.telegram-relay-{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)

os.chmod(path, 0o600)
print("CLIENTPLATFORM_TELEGRAM_RELAY_ENV_OK")
PY

BACKUP_ARGS=()
if ! grep -Eq '^CLIENTPLATFORM_BACKUP_AGE_RECIPIENT=age1[^[:space:]]+' "$ENV_FILE"; then
    BACKUP_ARGS=(--allow-local-backup)
fi

CLIENTPLATFORM_ROOT="$ROOT" \
CLIENTPLATFORM_TARGET_REF="main" \
CLIENTPLATFORM_EXPECTED_SHA="$EXPECTED_SHA" \
bash "$ROOT/deploy/clientplatform/update-production.sh" \
    --recover-unavailable-baseline \
    --timeout-seconds 420 \
    "${BACKUP_ARGS[@]}"

test "$(git -C "$ROOT" rev-parse HEAD)" = "$EXPECTED_SHA"
test "$(docker inspect "$APP" --format '{{.State.Running}}')" = "true"

docker exec -i "$APP" python - <<'PY'
from __future__ import annotations

import asyncio
import os

from core.telegram_bot import PollingAiohttpSession, build_bot


async def main() -> None:
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("CLIENTPLATFORM_TELEGRAM_RELAY_PROOF_FAILED:token_missing")

    bot = build_bot(token)
    try:
        if not isinstance(bot.session, PollingAiohttpSession):
            raise SystemExit(
                "CLIENTPLATFORM_TELEGRAM_RELAY_PROOF_FAILED:unexpected_session"
            )
        if bot.session.proxy_mode != "http_connect":
            raise SystemExit(
                "CLIENTPLATFORM_TELEGRAM_RELAY_PROOF_FAILED:relay_not_active"
            )
        me = await bot.get_me()
        webhook = await bot.get_webhook_info()
        if not me.is_bot:
            raise SystemExit(
                "CLIENTPLATFORM_TELEGRAM_RELAY_PROOF_FAILED:not_a_bot"
            )
        if webhook.url:
            raise SystemExit(
                "CLIENTPLATFORM_TELEGRAM_RELAY_PROOF_FAILED:webhook_enabled"
            )
        print(
            "CLIENTPLATFORM_TELEGRAM_RELAY_GETME_OK:"
            f"id={me.id} username={me.username or 'unknown'} mode=http_connect"
        )
    finally:
        await bot.session.close()


asyncio.run(main())
PY

STARTED_AT="$(docker inspect "$APP" --format '{{.State.StartedAt}}')"
POLLING_OK=0
for _ in $(seq 1 30); do
    if docker logs --since "$STARTED_AT" "$APP" 2>&1 \
        | grep -Fq "Run polling for bot @clientplatform_bot"; then
        POLLING_OK=1
        break
    fi
    test "$(docker inspect "$APP" --format '{{.State.Running}}')" = "true"
    sleep 3
done

[ "$POLLING_OK" -eq 1 ] || fail "polling_start_marker_missing"

printf 'CLIENTPLATFORM_TELEGRAM_RELAY_ACTIVATION_OK:sha=%s relay_http=%s\n' \
    "$EXPECTED_SHA" "$HTTP_CODE"
