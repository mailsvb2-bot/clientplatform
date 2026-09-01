#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE_DIR="${1:-}"
ENV_FILE="${CLIENTPLATFORM_ENV_FILE:-/etc/clientplatform/clientplatform.env}"
RUNTIME_ROOT="${CLIENTPLATFORM_RUNTIME_ROOT:-/var/lib/clientplatform/runtime}"
STATE_ROOT="${CLIENTPLATFORM_WRITABLE_ROOT:-$(dirname "$RUNTIME_ROOT")/state}"

if [ -z "$RELEASE_DIR" ] || [ ! -d "$RELEASE_DIR" ]; then
  echo "RELEASE_RUNTIME_COMPATIBILITY_FAILED release directory is unavailable" >&2
  exit 2
fi
RELEASE_DIR="$(readlink -f "$RELEASE_DIR")"
if [ ! -x "$RELEASE_DIR/.venv/bin/python" ]; then
  echo "RELEASE_RUNTIME_COMPATIBILITY_FAILED release Python is unavailable: $RELEASE_DIR" >&2
  exit 3
fi
if [ ! -f "$ENV_FILE" ] || [ -L "$ENV_FILE" ]; then
  echo "RELEASE_RUNTIME_COMPATIBILITY_FAILED authoritative env file is unavailable or unsafe: $ENV_FILE" >&2
  exit 4
fi

mkdir -p \
  "$STATE_ROOT/python-cache" \
  "$STATE_ROOT/xdg-cache" \
  "$STATE_ROOT/matplotlib" \
  "$STATE_ROOT/tmp" \
  "$STATE_ROOT/data" \
  "$STATE_ROOT/logs"

(
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a

  export PYTHONDONTWRITEBYTECODE=1
  export PYTHONPYCACHEPREFIX="$STATE_ROOT/python-cache"
  export XDG_CACHE_HOME="$STATE_ROOT/xdg-cache"
  export MPLCONFIGDIR="$STATE_ROOT/matplotlib"
  export TMPDIR="$STATE_ROOT/tmp"
  export CLIENTPLATFORM_WRITABLE_ROOT="$STATE_ROOT"
  export CLIENTPLATFORM_DATA_DIR="$STATE_ROOT/data"
  export CLIENTPLATFORM_LOGS_DIR="$STATE_ROOT/logs"

  cd "$RELEASE_DIR"
  "$RELEASE_DIR/.venv/bin/python" - <<'PY'
from services.schema import init_db
from services.validator import validate_all

init_db()
validate_all(strict=True)
print("RELEASE_RUNTIME_COMPATIBILITY_OK")
PY
)
