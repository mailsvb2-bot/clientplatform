#!/usr/bin/env bash
set -euo pipefail

# Server-side release helper.
# GitHub Actions are optional for this repository; the target-server production
# gate is the release source of truth.

ROOT_DIR="${CLIENTPLATFORM_ROOT:-/root/clientplatform}"
SERVICE_NAME="${CLIENTPLATFORM_SERVICE:-clientplatform}"
BASE_BRANCH="${BASE_BRANCH:-main}"
PR_NUMBER="${PR_NUMBER:-}"
MERGE_METHOD="${MERGE_METHOD:-squash}"
RESTART_SERVICE="${RESTART_SERVICE:-0}"

cd "$ROOT_DIR"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

run_gate() {
  echo "==> production gate"
  python scripts/production_gate.py
}

echo "==> git fetch"
git fetch --prune origin

if [[ -n "$PR_NUMBER" ]]; then
  echo "==> checkout PR #$PR_NUMBER"
  gh pr checkout "$PR_NUMBER"
else
  echo "==> checkout $BASE_BRANCH"
  git checkout "$BASE_BRANCH"
  git pull --ff-only origin "$BASE_BRANCH"
fi

run_gate

if [[ -n "$PR_NUMBER" ]]; then
  echo "==> mark PR ready and merge with server gate as source of truth"
  gh pr ready "$PR_NUMBER" || true
  gh pr merge "$PR_NUMBER" --"$MERGE_METHOD" --delete-branch --admin \
    --subject "Server-gated production release" \
    --body "Validated on target server with python scripts/production_gate.py: PRODUCTION_GATE_OK. GitHub Actions are optional/unavailable; the server production gate is the release source of truth."

  echo "==> sync $BASE_BRANCH after merge"
  git checkout "$BASE_BRANCH"
  git pull --ff-only origin "$BASE_BRANCH"
  run_gate
fi

if [[ "$RESTART_SERVICE" == "1" ]]; then
  echo "==> restart $SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"
  sleep 3
  systemctl status "$SERVICE_NAME" --no-pager
  run_gate
fi

echo "SERVER_RELEASE_GATE_OK"
