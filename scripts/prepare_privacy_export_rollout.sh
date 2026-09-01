#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SOURCE_DIR="${APP_DIR:-/root/clientplatform}"
ENV_FILE="${CLIENTPLATFORM_ENV_FILE:-/etc/clientplatform/clientplatform.env}"
SYSTEM_PYTHON="${SYSTEM_PYTHON:-/usr/bin/python3}"
PUBLIC_BASE_URL="${PRIVACY_EXPORT_DEFAULT_PUBLIC_BASE_URL:-https://clientplatform-bot.clientplatform.ru}"
TTL_MINUTES="${PRIVACY_EXPORT_DEFAULT_TTL_MINUTES:-10}"

if [ "$(id -u)" -ne 0 ]; then
  echo "PRIVACY_EXPORT_ROLLOUT_FAILED: run as root" >&2
  exit 2
fi
if [ ! -d "$SOURCE_DIR/.git" ]; then
  echo "PRIVACY_EXPORT_ROLLOUT_FAILED: source checkout not found: $SOURCE_DIR" >&2
  exit 3
fi
if [ ! -x "$SYSTEM_PYTHON" ]; then
  echo "PRIVACY_EXPORT_ROLLOUT_FAILED: system Python is unavailable" >&2
  exit 4
fi
if [ ! -f "$ENV_FILE" ]; then
  echo "PRIVACY_EXPORT_ROLLOUT_FAILED: authoritative env file not found: $ENV_FILE" >&2
  exit 5
fi

cd "$SOURCE_DIR"

"$SYSTEM_PYTHON" scripts/migrate_privacy_export_env.py \
  --env-file "$ENV_FILE" \
  --public-base-url "$PUBLIC_BASE_URL" \
  --ttl-minutes "$TTL_MINUTES"

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

PYTHONDONTWRITEBYTECODE=1 "$SYSTEM_PYTHON" scripts/runtime_contract.py

echo "PRIVACY_EXPORT_ROLLOUT_PREPARED env_file=$ENV_FILE restart_performed=0"
echo "Run the normal immutable deploy only after this command succeeds."
