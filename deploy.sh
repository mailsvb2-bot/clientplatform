#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${METROTHERAPY_ENV_FILE:-/etc/metrotherapy/metrotherapy.env}"
BOOTSTRAPPED_SHA="${DEPLOY_BOOTSTRAPPED_SHA:-}"
RECOVERY_SCRIPT="$SOURCE_DIR/scripts/repair_contaminated_current_release.sh"
CANDIDATE_PREPARER="$SOURCE_DIR/scripts/prepare_immutable_candidate.sh"
WRITE_GUARD_SCRIPT="$SOURCE_DIR/scripts/install_runtime_write_guard.sh"
RUNTIME_ROOT="${METRO_RUNTIME_ROOT:-/var/lib/metrotherapy/runtime}"
CURRENT_LINK="${METRO_CURRENT_RELEASE_LINK:-$RUNTIME_ROOT/current}"
STATE_ROOT="${METRO_WRITABLE_ROOT:-$(dirname "$RUNTIME_ROOT")/state}"
SERVICE_NAME="${SERVICE_NAME:-metrotherapy.service}"
LOCK_FILE="${LOCK_FILE:-$SOURCE_DIR/data/deploy/metrotherapy_deploy.lock}"
FLOCK_BIN="${FLOCK_BIN:-/usr/bin/flock}"
LOCK_WAIT_SECONDS="${DEPLOY_LOCK_WAIT_SECONDS:-900}"
DEPLOY_LOCK_HELD="${DEPLOY_LOCK_HELD:-0}"

if [ ! -f "$ENV_FILE" ]; then
  echo "IMMUTABLE_DEPLOY_FAILED production env file is missing: $ENV_FILE" >&2
  exit 2
fi
if [ ! -f "$RECOVERY_SCRIPT" ]; then
  echo "IMMUTABLE_DEPLOY_FAILED current-release recovery script is missing: $RECOVERY_SCRIPT" >&2
  exit 5
fi
if [ ! -f "$CANDIDATE_PREPARER" ]; then
  echo "IMMUTABLE_DEPLOY_FAILED candidate preparation script is missing: $CANDIDATE_PREPARER" >&2
  exit 6
fi
if [ ! -f "$WRITE_GUARD_SCRIPT" ]; then
  echo "IMMUTABLE_DEPLOY_FAILED runtime write guard is missing: $WRITE_GUARD_SCRIPT" >&2
  exit 7
fi
if [ ! -x "$FLOCK_BIN" ]; then
  echo "IMMUTABLE_DEPLOY_FAILED flock is unavailable: $FLOCK_BIN" >&2
  exit 8
fi
case "$LOCK_WAIT_SECONDS" in
  ''|*[!0-9]*)
    echo "IMMUTABLE_DEPLOY_FAILED DEPLOY_LOCK_WAIT_SECONDS must be a non-negative integer" >&2
    exit 9
    ;;
esac

mkdir -p \
  "$STATE_ROOT/python-cache" \
  "$STATE_ROOT/xdg-cache" \
  "$STATE_ROOT/matplotlib" \
  "$STATE_ROOT/tmp"
export METRO_WRITABLE_ROOT="$STATE_ROOT"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$STATE_ROOT/python-cache"
export XDG_CACHE_HOME="$STATE_ROOT/xdg-cache"
export MPLCONFIGDIR="$STATE_ROOT/matplotlib"
export TMPDIR="$STATE_ROOT/tmp"
export GIT_TERMINAL_PROMPT=0

acquire_deploy_lock() {
  local parent_lock=""
  local canonical_lock=""

  if [ "$DEPLOY_LOCK_HELD" = "1" ]; then
    return 0
  fi

  mkdir -p "$(dirname "$LOCK_FILE")"
  touch "$LOCK_FILE"
  canonical_lock="$(readlink -f "$LOCK_FILE")"
  parent_lock="$(readlink -f "/proc/$PPID/fd/9" 2>/dev/null || true)"

  # run_deploy_worker.sh already owns FD 9. Its synchronous child inherits the
  # protection through the parent lifetime and must not deadlock by reopening it.
  if [ -n "$parent_lock" ] && [ "$parent_lock" = "$canonical_lock" ]; then
    export DEPLOY_LOCK_HELD=1
    echo "=== deploy lock inherited from worker parent=$PPID ==="
    return 0
  fi

  exec 8<>"$LOCK_FILE"
  echo "=== deploy waiting for entrypoint lock timeout=${LOCK_WAIT_SECONDS}s ==="
  if ! "$FLOCK_BIN" -w "$LOCK_WAIT_SECONDS" 8; then
    echo "IMMUTABLE_DEPLOY_FAILED deploy lock wait timed out after ${LOCK_WAIT_SECONDS}s" >&2
    return 1
  fi
  export DEPLOY_LOCK_HELD=1
  echo "=== deploy entrypoint lock acquired pid=$$ ==="
}

restore_runtime_after_failure() {
  local recovered_release=""
  if ! bash "$RECOVERY_SCRIPT" repair "$SOURCE_DIR"; then
    echo "IMMUTABLE_DEPLOY_RECOVERY_FAILED current release repair failed" >&2
    return 1
  fi
  recovered_release="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
  [ -n "$recovered_release" ] && [ -d "$recovered_release" ] || {
    echo "IMMUTABLE_DEPLOY_RECOVERY_FAILED current release is unresolved after repair" >&2
    return 1
  }
  if ! bash "$WRITE_GUARD_SCRIPT" for-release "$recovered_release"; then
    echo "IMMUTABLE_DEPLOY_RECOVERY_FAILED compatible runtime guard selection failed" >&2
    return 1
  fi
  /usr/bin/systemctl restart "$SERVICE_NAME"
}

acquire_deploy_lock
bash "$SOURCE_DIR/scripts/check_remote_main_topology.sh" "$SOURCE_DIR"

git -C "$SOURCE_DIR" checkout main
BEFORE_SHA="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
git -C "$SOURCE_DIR" fetch --prune origin main
git -C "$SOURCE_DIR" merge --ff-only origin/main
AFTER_SHA="$(git -C "$SOURCE_DIR" rev-parse HEAD)"

if [ "$BEFORE_SHA" != "$AFTER_SHA" ]; then
  if [ "$BOOTSTRAPPED_SHA" = "$AFTER_SHA" ]; then
    echo "IMMUTABLE_DEPLOY_FAILED deploy wrapper self-reexec loop at $AFTER_SHA" >&2
    exit 3
  fi
  echo "=== deploy wrapper updated old=$BEFORE_SHA new=$AFTER_SHA; re-exec updated wrapper ==="
  exec env \
    DEPLOY_BOOTSTRAPPED_SHA="$AFTER_SHA" \
    DEPLOY_LOCK_HELD=1 \
    METROTHERAPY_ENV_FILE="$ENV_FILE" \
    METRO_RUNTIME_ROOT="$RUNTIME_ROOT" \
    METRO_CURRENT_RELEASE_LINK="$CURRENT_LINK" \
    METRO_WRITABLE_ROOT="$STATE_ROOT" \
    bash "$SOURCE_DIR/deploy.sh" "$@"
fi

if [ -n "$BOOTSTRAPPED_SHA" ] && [ "$BOOTSTRAPPED_SHA" != "$AFTER_SHA" ]; then
  echo "IMMUTABLE_DEPLOY_FAILED bootstrap SHA mismatch expected=$BOOTSTRAPPED_SHA actual=$AFTER_SHA" >&2
  exit 4
fi

bash "$WRITE_GUARD_SCRIPT" enforce
bash "$RECOVERY_SCRIPT" repair "$SOURCE_DIR"
bash "$CANDIDATE_PREPARER" "$SOURCE_DIR"
if bash "$SOURCE_DIR/scripts/immutable_deploy.sh" "$@"; then
  bash "$RECOVERY_SCRIPT" cleanup "$SOURCE_DIR"
else
  code="$?"
  restore_runtime_after_failure || true
  exit "$code"
fi
