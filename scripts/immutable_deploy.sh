#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="${APP_DIR:-/root/metrotherapy}"
SERVICE_NAME="${SERVICE_NAME:-metrotherapy.service}"
DEPLOY_WEBHOOK_SERVICE="${DEPLOY_WEBHOOK_SERVICE:-github-deploy-webhook.service}"
DEPLOY_WEBHOOK_SOURCE="$SOURCE_DIR/ops/deploy_webhook.py"
DEPLOY_WEBHOOK_TARGET="${DEPLOY_WEBHOOK_TARGET:-/root/deploy_webhook.py}"
ENV_FILE="${METROTHERAPY_ENV_FILE:-/etc/metrotherapy/metrotherapy.env}"
SYSTEM_PYTHON="${SYSTEM_PYTHON:-/usr/bin/python3}"
ZERO_SHA="0000000000000000000000000000000000000000"
TRIGGER_SHA="${DEPLOY_TRIGGER_SHA:-}"

cd "$SOURCE_DIR"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
else
  echo "WARNING: production env file not found: $ENV_FILE"
fi

RUNTIME_ROOT="${METRO_RUNTIME_ROOT:-/var/lib/metrotherapy/runtime}"
RELEASES_DIR="${METRO_RELEASES_DIR:-$RUNTIME_ROOT/releases}"
CURRENT_LINK="${METRO_CURRENT_RELEASE_LINK:-$RUNTIME_ROOT/current}"
PREVIOUS_LINK="${METRO_PREVIOUS_RELEASE_LINK:-$RUNTIME_ROOT/previous}"
DEPLOY_STATE_DIR="${DEPLOY_STATE_DIR:-/var/lib/metrotherapy/deploy-state}"
DEPLOYED_SHA_FILE="${DEPLOYED_SHA_FILE:-$DEPLOY_STATE_DIR/deployed_sha}"
DEPLOYMENT_PROOF_FILE="${DEPLOYMENT_PROOF_FILE:-$DEPLOY_STATE_DIR/deployment-proof.json}"
SYSTEMD_OVERRIDE="${METRO_IMMUTABLE_SYSTEMD_OVERRIDE:-/etc/systemd/system/$SERVICE_NAME.d/zz-immutable-release.conf}"
RELEASE_MANAGER="$SOURCE_DIR/scripts/immutable_release.py"
RELEASE_BUILDER="$SOURCE_DIR/scripts/build_immutable_release.sh"
ENV_MIGRATOR="$SOURCE_DIR/scripts/migrate_privacy_export_env.py"
RUNTIME_CONTRACT="$SOURCE_DIR/scripts/runtime_contract.py"
PRIVACY_EXPORT_DEFAULT_PUBLIC_BASE_URL="${PRIVACY_EXPORT_DEFAULT_PUBLIC_BASE_URL:-https://metrotherapy-bot.metrotherapy.ru}"
PRIVACY_EXPORT_DEFAULT_TTL_MINUTES="${PRIVACY_EXPORT_DEFAULT_TTL_MINUTES:-10}"
LOCAL_HEALTH_URL="${LOCAL_HEALTH_URL:-http://127.0.0.1:8082/healthz}"
LOCAL_READY_URL="${LOCAL_READY_URL:-http://127.0.0.1:8082/readyz}"
PUBLIC_HEALTH_URL="${PUBLIC_HEALTH_URL:-https://metrotherapy-bot.metrotherapy.ru/healthz}"
TIMEOUT_BIN="${TIMEOUT_BIN:-/usr/bin/timeout}"
GIT_NETWORK_TIMEOUT_SECONDS="${GIT_NETWORK_TIMEOUT_SECONDS:-180}"
WEBHOOK_RESTART_TIMEOUT_SECONDS="${WEBHOOK_RESTART_TIMEOUT_SECONDS:-120}"
RELEASE_BUILD_TIMEOUT_SECONDS="${RELEASE_BUILD_TIMEOUT_SECONDS:-1200}"
VALIDATOR_TIMEOUT_SECONDS="${VALIDATOR_TIMEOUT_SECONDS:-600}"
SCHEMA_COMPAT_TIMEOUT_SECONDS="${SCHEMA_COMPAT_TIMEOUT_SECONDS:-600}"
SERVICE_RESTART_TIMEOUT_SECONDS="${SERVICE_RESTART_TIMEOUT_SECONDS:-120}"
HEALTH_WAIT_SECONDS="${HEALTH_WAIT_SECONDS:-90}"
PRODUCTION_GATE_TIMEOUT_SECONDS="${PRODUCTION_GATE_TIMEOUT_SECONDS:-3600}"
RELEASE_RETENTION="${RELEASE_RETENTION:-5}"
SWITCHED=0
NEW_SHA=""
OLD_RUNTIME_SHA=""

is_valid_sha() {
  case "${1:-}" in
    ''|*[!0-9a-f]*) return 1 ;;
  esac
  [ "${#1}" -eq 40 ] && [ "$1" != "$ZERO_SHA" ]
}

is_positive_integer() {
  case "${1:-}" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$1" -gt 0 ]
}

for timeout_pair in \
  "GIT_NETWORK_TIMEOUT_SECONDS:$GIT_NETWORK_TIMEOUT_SECONDS" \
  "WEBHOOK_RESTART_TIMEOUT_SECONDS:$WEBHOOK_RESTART_TIMEOUT_SECONDS" \
  "RELEASE_BUILD_TIMEOUT_SECONDS:$RELEASE_BUILD_TIMEOUT_SECONDS" \
  "VALIDATOR_TIMEOUT_SECONDS:$VALIDATOR_TIMEOUT_SECONDS" \
  "SCHEMA_COMPAT_TIMEOUT_SECONDS:$SCHEMA_COMPAT_TIMEOUT_SECONDS" \
  "SERVICE_RESTART_TIMEOUT_SECONDS:$SERVICE_RESTART_TIMEOUT_SECONDS" \
  "HEALTH_WAIT_SECONDS:$HEALTH_WAIT_SECONDS" \
  "PRODUCTION_GATE_TIMEOUT_SECONDS:$PRODUCTION_GATE_TIMEOUT_SECONDS"
do
  name="${timeout_pair%%:*}"
  value="${timeout_pair#*:}"
  if ! is_positive_integer "$value"; then
    echo "IMMUTABLE_DEPLOY_FAILED $name must be a positive integer" >&2
    exit 20
  fi
done
if ! is_positive_integer "$RELEASE_RETENTION"; then
  echo "IMMUTABLE_DEPLOY_FAILED RELEASE_RETENTION must be positive" >&2
  exit 21
fi
if [ ! -x "$TIMEOUT_BIN" ] || [ ! -x "$SYSTEM_PYTHON" ]; then
  echo "IMMUTABLE_DEPLOY_FAILED required executable is unavailable" >&2
  exit 22
fi
if [ ! -f "$RELEASE_MANAGER" ] || [ ! -f "$RELEASE_BUILDER" ]; then
  echo "IMMUTABLE_DEPLOY_FAILED release tooling is missing" >&2
  exit 23
fi

run_bounded() {
  local seconds="$1"
  local label="$2"
  local code
  shift 2
  echo "=== bounded command: $label timeout=${seconds}s ==="
  if "$TIMEOUT_BIN" --signal=TERM --kill-after=30s "$seconds" "$@"; then
    return 0
  else
    code="$?"
  fi
  echo "IMMUTABLE_DEPLOY_FAILED command=$label code=$code" >&2
  return "$code"
}

reload_authoritative_environment() {
  [ -f "$ENV_FILE" ] || {
    echo "IMMUTABLE_DEPLOY_FAILED authoritative env file not found: $ENV_FILE" >&2
    return 1
  }
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
}

migrate_privacy_export_environment() {
  [ -f "$ENV_MIGRATOR" ] || {
    echo "IMMUTABLE_DEPLOY_FAILED privacy export env migrator is missing" >&2
    return 1
  }
  [ -f "$RUNTIME_CONTRACT" ] || {
    echo "IMMUTABLE_DEPLOY_FAILED runtime contract checker is missing" >&2
    return 1
  }
  run_bounded "$VALIDATOR_TIMEOUT_SECONDS"     "migrate authoritative privacy export environment"     "$SYSTEM_PYTHON" "$ENV_MIGRATOR"       --env-file "$ENV_FILE"       --public-base-url "$PRIVACY_EXPORT_DEFAULT_PUBLIC_BASE_URL"       --ttl-minutes "$PRIVACY_EXPORT_DEFAULT_TTL_MINUTES"
  reload_authoritative_environment
  run_bounded "$VALIDATOR_TIMEOUT_SECONDS"     "validate production runtime contract after env migration"     env PYTHONDONTWRITEBYTECODE=1 "$SYSTEM_PYTHON" "$RUNTIME_CONTRACT"
}

wait_for_health() {
  local url="$1"
  local timeout_seconds="$2"
  local start_ts now_ts elapsed
  start_ts="$(date +%s)"
  echo "=== wait for health: $url timeout=${timeout_seconds}s ==="
  while true; do
    if curl -fsS --max-time 5 "$url" >/dev/null; then
      echo "=== health OK: $url ==="
      return 0
    fi
    now_ts="$(date +%s)"
    elapsed="$((now_ts - start_ts))"
    if [ "$elapsed" -ge "$timeout_seconds" ]; then
      echo "IMMUTABLE_DEPLOY_FAILED health timeout: $url" >&2
      return 1
    fi
    sleep 2
  done
}

release_sha_from_link() {
  "$SYSTEM_PYTHON" "$RELEASE_MANAGER" inspect "$1" --required \
    | "$SYSTEM_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["sha"])'
}

release_path_from_link() {
  "$SYSTEM_PYTHON" "$RELEASE_MANAGER" inspect "$1" --required \
    | "$SYSTEM_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["path"])'
}

record_successful_deployed_sha() {
  local sha="$1"
  local temp
  is_valid_sha "$sha" || return 1
  mkdir -p "$DEPLOY_STATE_DIR"
  temp="$(mktemp "$DEPLOY_STATE_DIR/deployed_sha.XXXXXX")"
  printf '%s\n' "$sha" > "$temp"
  chmod 0644 "$temp"
  mv -f "$temp" "$DEPLOYED_SHA_FILE"
}

read_recorded_sha() {
  local value=""
  [ -f "$DEPLOYED_SHA_FILE" ] || return 1
  IFS= read -r value < "$DEPLOYED_SHA_FILE" || true
  is_valid_sha "$value" || return 1
  printf '%s\n' "$value"
}

require_telegram_polling_contract() {
  local transport run_mode
  transport="$(printf '%s' "${TELEGRAM_TRANSPORT:-polling}" | tr '[:upper:]' '[:lower:]')"
  run_mode="$(printf '%s' "${RUN_MODE:-}" | tr '[:upper:]' '[:lower:]')"
  [ "$transport" = "polling" ] || { echo "Telegram production transport must stay polling" >&2; return 1; }
  [ -z "$run_mode" ] || [ "$run_mode" = "polling" ] || { echo "RUN_MODE must stay polling" >&2; return 1; }
  case "$(printf '%s' "${TELEGRAM_WEBHOOK_ENABLED:-0}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on|webhook) echo "Telegram webhook must stay disabled" >&2; return 1 ;;
  esac
  case "$(printf '%s' "${TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED:-0}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on|webhook) echo "Telegram legacy webhook must stay disabled" >&2; return 1 ;;
  esac
}

require_single_local_main_branch() {
  local branch branch_list branch_count
  while IFS= read -r branch; do
    [ -n "$branch" ] || continue
    if [ "$branch" != "main" ]; then
      git branch -D "$branch"
    fi
  done < <(git for-each-ref --format='%(refname:short)' refs/heads)
  branch_list="$(git for-each-ref --format='%(refname:short)' refs/heads)"
  branch_count="$(printf '%s\n' "$branch_list" | sed '/^$/d' | wc -l | tr -d ' ')"
  [ "$branch_count" = "1" ] && [ "$branch_list" = "main" ] || {
    echo "IMMUTABLE_DEPLOY_FAILED production Git topology is not 1/main" >&2
    return 1
  }
}

sync_deploy_webhook_service() {
  [ -f "$DEPLOY_WEBHOOK_SOURCE" ] || return 0
  install -m 0644 "$DEPLOY_WEBHOOK_SOURCE" "$DEPLOY_WEBHOOK_TARGET"
  if systemctl list-unit-files "$DEPLOY_WEBHOOK_SERVICE" >/dev/null 2>&1; then
    run_bounded "$WEBHOOK_RESTART_TIMEOUT_SECONDS" \
      "restart deploy webhook service" \
      systemctl restart "$DEPLOY_WEBHOOK_SERVICE"
  fi
}

install_immutable_systemd_override() {
  local temp changed=0
  mkdir -p "$(dirname "$SYSTEMD_OVERRIDE")"
  temp="$(mktemp "$(dirname "$SYSTEMD_OVERRIDE")/.immutable-release.XXXXXX")"
  cat > "$temp" <<EOF
[Service]
WorkingDirectory=$CURRENT_LINK
ExecStart=
ExecStart=$CURRENT_LINK/.venv/bin/python $CURRENT_LINK/main.py
Environment=PYTHONDONTWRITEBYTECODE=1
EOF
  chmod 0644 "$temp"
  if [ ! -f "$SYSTEMD_OVERRIDE" ] || ! cmp -s "$temp" "$SYSTEMD_OVERRIDE"; then
    mv -f "$temp" "$SYSTEMD_OVERRIDE"
    changed=1
  else
    rm -f "$temp"
  fi
  systemctl daemon-reload
  printf '%s\n' "$changed"
}

verify_immutable_systemd_effective_config() {
  local working_directory exec_start
  working_directory="$(systemctl show "$SERVICE_NAME" --property=WorkingDirectory --value)"
  exec_start="$(systemctl show "$SERVICE_NAME" --property=ExecStart --value)"

  if [ "$working_directory" != "$CURRENT_LINK" ]; then
    echo "IMMUTABLE_DEPLOY_FAILED effective WorkingDirectory is not immutable current: $working_directory" >&2
    return 1
  fi
  case "$exec_start" in
    *"$CURRENT_LINK/.venv/bin/python"*"$CURRENT_LINK/main.py"*) ;;
    *)
      echo "IMMUTABLE_DEPLOY_FAILED effective ExecStart is not immutable current: $exec_start" >&2
      return 1
      ;;
  esac
}

build_release() {
  local sha="$1"
  run_bounded "$RELEASE_BUILD_TIMEOUT_SECONDS" \
    "build immutable release $sha" \
    env SOURCE_DIR="$SOURCE_DIR" RUNTIME_ROOT="$RUNTIME_ROOT" RELEASES_DIR="$RELEASES_DIR" \
      SYSTEM_PYTHON="$SYSTEM_PYTHON" SHARED_AUDIO_DIR="${SHARED_AUDIO_DIR:-$SOURCE_DIR/audio}" \
      bash "$RELEASE_BUILDER" "$sha"
}

validate_release() {
  "$SYSTEM_PYTHON" "$RELEASE_MANAGER" validate "$1" >/dev/null
}

validate_candidate_and_expand_schema() {
  local release="$1"
  run_bounded "$VALIDATOR_TIMEOUT_SECONDS" \
    "candidate strict validator and expand migrations" \
    env PYTHONDONTWRITEBYTECODE=1 VALIDATOR_RELEASE_MODE=1 VALIDATOR_STRICT=1 \
      VALIDATOR_GUARDRAILS_STRICT=1 \
      "$release/.venv/bin/python" "$release/scripts/validate_project.py"
  validate_release "$release"
}

verify_previous_release_on_expanded_schema() {
  local release="$1"
  local command
  command="from services.schema import init_db; init_db(); "
  command+="from services.db.schema.readiness import schema_readiness; "
  command+="ok,error=schema_readiness(); "
  command+="assert ok, error; print('PREVIOUS_RELEASE_EXPANDED_SCHEMA_OK')"
  run_bounded "$SCHEMA_COMPAT_TIMEOUT_SECONDS" \
    "previous release compatibility on expanded schema" \
    env PYTHONDONTWRITEBYTECODE=1 \
      "$release/.venv/bin/python" -c "$command"
  validate_release "$release"
}

restart_runtime_and_wait() {
  run_bounded "$SERVICE_RESTART_TIMEOUT_SECONDS" \
    "restart metrotherapy service" \
    systemctl restart "$SERVICE_NAME"
  wait_for_health "$LOCAL_HEALTH_URL" "$HEALTH_WAIT_SECONDS"
  wait_for_health "$LOCAL_READY_URL" "$HEALTH_WAIT_SECONDS"
}

rollback() {
  local code="${1:-1}"
  trap - ERR TERM INT HUP
  echo "=== immutable deploy failed code=$code at $(date -Is) ===" >&2
  if [ "$SWITCHED" -eq 1 ]; then
    echo "=== atomically roll current back to previous release ===" >&2
    "$SYSTEM_PYTHON" "$RELEASE_MANAGER" rollback \
      --current-link "$CURRENT_LINK" \
      --previous-link "$PREVIOUS_LINK" || true
    "$TIMEOUT_BIN" --signal=TERM --kill-after=15s \
      "$SERVICE_RESTART_TIMEOUT_SECONDS" \
      systemctl restart "$SERVICE_NAME" || true
    wait_for_health "$LOCAL_HEALTH_URL" "$HEALTH_WAIT_SECONDS" || true
    "$SYSTEM_PYTHON" "$RELEASE_MANAGER" inspect "$CURRENT_LINK" --required || true
  fi
  systemctl status "$SERVICE_NAME" --no-pager -l | sed -n '1,80p' || true
  exit "$code"
}

cleanup_old_releases() {
  local current_path previous_path keep path count=0
  current_path="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
  previous_path="$(readlink -f "$PREVIOUS_LINK" 2>/dev/null || true)"
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    if [ "$path" = "$current_path" ] || [ "$path" = "$previous_path" ]; then
      continue
    fi
    count="$((count + 1))"
    if [ "$count" -gt "$RELEASE_RETENTION" ]; then
      rm -rf --one-file-system "$path"
    fi
  done < <(find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -name '[0-9a-f]*' -printf '%T@ %p\n' \
    | sort -nr | awk '{print $2}')
}

trap 'rollback $?' ERR
trap 'rollback 143' TERM
trap 'rollback 130' INT
trap 'rollback 129' HUP

export GIT_TERMINAL_PROMPT=0
export GIT_HTTP_LOW_SPEED_LIMIT="${GIT_HTTP_LOW_SPEED_LIMIT:-1}"
export GIT_HTTP_LOW_SPEED_TIME="${GIT_HTTP_LOW_SPEED_TIME:-30}"

require_telegram_polling_contract
[ -z "$(git status --porcelain)" ] || { echo "IMMUTABLE_DEPLOY_FAILED dirty source worktree" >&2; exit 10; }
git checkout main
OLD_SOURCE_SHA="$(git rev-parse HEAD)"
run_bounded "$GIT_NETWORK_TIMEOUT_SECONDS" "fetch origin" git fetch --prune origin
git merge --ff-only origin/main
migrate_privacy_export_environment
require_single_local_main_branch
NEW_SHA="$(git rev-parse HEAD)"
echo "=== immutable deploy source old=$OLD_SOURCE_SHA new=$NEW_SHA ==="

if [ -n "$TRIGGER_SHA" ]; then
  is_valid_sha "$TRIGGER_SHA" || { echo "IMMUTABLE_DEPLOY_FAILED invalid trigger SHA" >&2; exit 24; }
  if recorded_sha="$(read_recorded_sha 2>/dev/null)"; then
    if git merge-base --is-ancestor "$TRIGGER_SHA" "$recorded_sha"; then
      echo "=== deploy coalesced trigger=$TRIGGER_SHA deployed=$recorded_sha ==="
      trap - ERR TERM INT HUP
      exit 0
    fi
  fi
fi

mkdir -p "$RUNTIME_ROOT" "$RELEASES_DIR" "$DEPLOY_STATE_DIR"

BOOTSTRAP_CURRENT=0
if [ -L "$CURRENT_LINK" ]; then
  OLD_RUNTIME_SHA="$(release_sha_from_link "$CURRENT_LINK")"
else
  if recorded_sha="$(read_recorded_sha 2>/dev/null)" && git cat-file -e "$recorded_sha^{commit}" 2>/dev/null; then
    OLD_RUNTIME_SHA="$recorded_sha"
  else
    OLD_RUNTIME_SHA="$OLD_SOURCE_SHA"
  fi
  build_release "$OLD_RUNTIME_SHA"
  "$SYSTEM_PYTHON" "$RELEASE_MANAGER" switch \
    --release-dir "$RELEASES_DIR/$OLD_RUNTIME_SHA" \
    --current-link "$CURRENT_LINK" \
    --previous-link "$PREVIOUS_LINK" >/dev/null
  BOOTSTRAP_CURRENT=1
fi

CURRENT_RELEASE_DIR="$(release_path_from_link "$CURRENT_LINK")"
validate_release "$CURRENT_RELEASE_DIR"
SYSTEMD_CHANGED="$(install_immutable_systemd_override)"
verify_immutable_systemd_effective_config
if [ "$BOOTSTRAP_CURRENT" -eq 1 ] || [ "$SYSTEMD_CHANGED" -eq 1 ]; then
  echo "=== bootstrap systemd on immutable current=$OLD_RUNTIME_SHA ==="
  restart_runtime_and_wait
fi

sync_deploy_webhook_service
build_release "$NEW_SHA"
CANDIDATE_DIR="$RELEASES_DIR/$NEW_SHA"
validate_candidate_and_expand_schema "$CANDIDATE_DIR"
verify_previous_release_on_expanded_schema "$CURRENT_RELEASE_DIR"

if [ "$NEW_SHA" != "$OLD_RUNTIME_SHA" ]; then
  "$SYSTEM_PYTHON" "$RELEASE_MANAGER" switch \
    --release-dir "$CANDIDATE_DIR" \
    --current-link "$CURRENT_LINK" \
    --previous-link "$PREVIOUS_LINK" >/dev/null
  SWITCHED=1
fi

restart_runtime_and_wait
wait_for_health "$PUBLIC_HEALTH_URL" "$HEALTH_WAIT_SECONDS"

run_bounded "$PRODUCTION_GATE_TIMEOUT_SECONDS" \
  "mandatory production backup, restore and readiness gate" \
  "$CURRENT_LINK/.venv/bin/python" "$CURRENT_LINK/scripts/production_gate.py" \
    --env-file "$ENV_FILE" \
    --health-url "$LOCAL_HEALTH_URL" \
    --ready-url "$LOCAL_READY_URL"

validate_release "$(release_path_from_link "$CURRENT_LINK")"
if [ -L "$PREVIOUS_LINK" ]; then
  validate_release "$(release_path_from_link "$PREVIOUS_LINK")"
fi

"$SYSTEM_PYTHON" "$RELEASE_MANAGER" write-proof \
  --proof-file "$DEPLOYMENT_PROOF_FILE" \
  --current-link "$CURRENT_LINK" \
  --previous-link "$PREVIOUS_LINK" \
  --production-gate "PRODUCTION_GATE_OK" \
  --health-url "$LOCAL_HEALTH_URL" \
  --readiness-url "$LOCAL_READY_URL" >/dev/null
record_successful_deployed_sha "$NEW_SHA"
cleanup_old_releases

trap - ERR TERM INT HUP
echo "IMMUTABLE_DEPLOY_OK sha=$NEW_SHA release=$CANDIDATE_DIR previous=$OLD_RUNTIME_SHA proof=$DEPLOYMENT_PROOF_FILE"
