#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${1:-enforce}"
RELEASE_PATH="${2:-}"
SERVICE_NAME="${SERVICE_NAME:-clientplatform.service}"
RUNTIME_ROOT="${CLIENTPLATFORM_RUNTIME_ROOT:-/var/lib/clientplatform/runtime}"
STATE_ROOT="${CLIENTPLATFORM_WRITABLE_ROOT:-$(dirname "$RUNTIME_ROOT")/state}"
DROPIN="${CLIENTPLATFORM_RUNTIME_WRITE_GUARD_OVERRIDE:-/etc/systemd/system/$SERVICE_NAME.d/zzz-runtime-write-guard.conf}"
SYSTEMCTL="${SYSTEMCTL:-/usr/bin/systemctl}"
CONTRACT_MARKER="${CLIENTPLATFORM_RUNTIME_STATE_CONTRACT_MARKER:-runtime/RUNTIME_STATE_CONTRACT_V1}"

for required in "$RUNTIME_ROOT" "$STATE_ROOT" "$(dirname "$DROPIN")"; do
  [ -n "$required" ] || {
    echo "RUNTIME_WRITE_GUARD_FAILED empty required path" >&2
    exit 10
  }
done

case "$STATE_ROOT" in
  "$RUNTIME_ROOT"|"$RUNTIME_ROOT"/*)
    echo "RUNTIME_WRITE_GUARD_FAILED writable state must be outside immutable runtime: $STATE_ROOT" >&2
    exit 11
    ;;
esac

EFFECTIVE_MODE="$MODE"
RESOLVED_RELEASE=""
case "$MODE" in
  enforce|compatibility) ;;
  for-release)
    [ -n "$RELEASE_PATH" ] || {
      echo "RUNTIME_WRITE_GUARD_FAILED for-release requires a release path" >&2
      exit 12
    }
    RESOLVED_RELEASE="$(readlink -f "$RELEASE_PATH" 2>/dev/null || true)"
    [ -n "$RESOLVED_RELEASE" ] && [ -d "$RESOLVED_RELEASE" ] || {
      echo "RUNTIME_WRITE_GUARD_FAILED unresolved release path: $RELEASE_PATH" >&2
      exit 13
    }
    case "$RESOLVED_RELEASE" in
      "$RUNTIME_ROOT"/*) ;;
      *)
        echo "RUNTIME_WRITE_GUARD_FAILED release must be inside runtime root: $RESOLVED_RELEASE" >&2
        exit 14
        ;;
    esac
    if [ -f "$RESOLVED_RELEASE/$CONTRACT_MARKER" ]; then
      EFFECTIVE_MODE="enforce"
    else
      EFFECTIVE_MODE="compatibility"
    fi
    ;;
  *)
    echo "usage: $0 {enforce|compatibility|for-release <release-path>}" >&2
    exit 2
    ;;
esac

mkdir -p \
  "$STATE_ROOT/data" \
  "$STATE_ROOT/logs" \
  "$STATE_ROOT/python-cache" \
  "$STATE_ROOT/xdg-cache" \
  "$STATE_ROOT/matplotlib" \
  "$STATE_ROOT/tmp" \
  "$(dirname "$DROPIN")"
chmod 0750 \
  "$STATE_ROOT" \
  "$STATE_ROOT/data" \
  "$STATE_ROOT/logs" \
  "$STATE_ROOT/python-cache" \
  "$STATE_ROOT/xdg-cache" \
  "$STATE_ROOT/matplotlib" \
  "$STATE_ROOT/tmp"

temp="$(mktemp "$(dirname "$DROPIN")/.runtime-write-guard.XXXXXX")"
cleanup() {
  rm -f "$temp"
}
trap cleanup EXIT TERM INT HUP

cat > "$temp" <<EOF
[Service]
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONPYCACHEPREFIX=$STATE_ROOT/python-cache
Environment=XDG_CACHE_HOME=$STATE_ROOT/xdg-cache
Environment=MPLCONFIGDIR=$STATE_ROOT/matplotlib
Environment=TMPDIR=$STATE_ROOT/tmp
Environment=CLIENTPLATFORM_WRITABLE_ROOT=$STATE_ROOT
Environment=CLIENTPLATFORM_DATA_DIR=$STATE_ROOT/data
Environment=CLIENTPLATFORM_LOGS_DIR=$STATE_ROOT/logs
ReadOnlyPaths=
ReadWritePaths=
EOF

if [ "$EFFECTIVE_MODE" = "enforce" ]; then
  cat >> "$temp" <<EOF
ReadOnlyPaths=$RUNTIME_ROOT
ReadWritePaths=$STATE_ROOT
EOF
fi

chmod 0644 "$temp"

if [ ! -f "$DROPIN" ] || ! cmp -s "$temp" "$DROPIN"; then
  mv -f "$temp" "$DROPIN"
  trap - EXIT TERM INT HUP
  "$SYSTEMCTL" daemon-reload
  echo "RUNTIME_WRITE_GUARD_INSTALLED mode=$EFFECTIVE_MODE dropin=$DROPIN runtime=$RUNTIME_ROOT state=$STATE_ROOT release=${RESOLVED_RELEASE:-none}"
else
  "$SYSTEMCTL" daemon-reload
  echo "RUNTIME_WRITE_GUARD_OK mode=$EFFECTIVE_MODE dropin=$DROPIN runtime=$RUNTIME_ROOT state=$STATE_ROOT release=${RESOLVED_RELEASE:-none}"
fi
