#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="${1:-${APP_DIR:-/root/clientplatform}}"
TIMEOUT_BIN="${TIMEOUT_BIN:-/usr/bin/timeout}"
GIT_NETWORK_TIMEOUT_SECONDS="${GIT_NETWORK_TIMEOUT_SECONDS:-180}"

case "$GIT_NETWORK_TIMEOUT_SECONDS" in
  ''|*[!0-9]*)
    echo "REMOTE_TOPOLOGY_FAILED invalid timeout" >&2
    exit 2
    ;;
esac
[ "$GIT_NETWORK_TIMEOUT_SECONDS" -gt 0 ] || exit 2
[ -x "$TIMEOUT_BIN" ] || { echo "REMOTE_TOPOLOGY_FAILED timeout utility missing" >&2; exit 3; }

branches="$($TIMEOUT_BIN --signal=TERM --kill-after=10s "$GIT_NETWORK_TIMEOUT_SECONDS" \
  git -C "$SOURCE_DIR" ls-remote --heads origin \
  | awk '{ref=$2; sub("^refs/heads/", "", ref); print ref}' \
  | sort)"
count="$(printf '%s\n' "$branches" | sed '/^$/d' | wc -l | tr -d ' ')"
if [ "$count" != "1" ] || [ "$branches" != "main" ]; then
  echo "REMOTE_TOPOLOGY_FAILED expected=1/main count=$count branches=$branches" >&2
  exit 4
fi

echo "REMOTE_TOPOLOGY_OK count=1 branches=main"
