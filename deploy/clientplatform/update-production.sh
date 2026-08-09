#!/bin/sh
set -eu

ROOT=${CLIENTPLATFORM_ROOT:-/opt/clientplatform}
REPOSITORY=${CLIENTPLATFORM_REPOSITORY:-https://github.com/mailsvb2-bot/clientplatform.git}
TARGET_REF=${CLIENTPLATFORM_TARGET_REF:-main}
EXPECTED_SHA=${CLIENTPLATFORM_EXPECTED_SHA:-}
APP_CONTAINER=clientplatform-production-app-1
EVIDENCE=/var/lib/clientplatform/deploy-evidence/latest.json
DEPLOY_LOCK=/run/lock/clientplatform-production-deploy.lock
STABILITY_SECONDS=${CLIENTPLATFORM_POST_DEPLOY_STABILITY_SECONDS:-20}

if [ "$(id -u)" -ne 0 ]; then
    echo "CLIENTPLATFORM_UPDATE_FAILED:root_required" >&2
    exit 1
fi

case "$STABILITY_SECONDS" in
    ''|*[!0-9]*)
        echo "CLIENTPLATFORM_UPDATE_FAILED:invalid_stability_seconds" >&2
        exit 1
        ;;
esac
if [ "$STABILITY_SECONDS" -lt 10 ] || [ "$STABILITY_SECONDS" -gt 120 ]; then
    echo "CLIENTPLATFORM_UPDATE_FAILED:invalid_stability_seconds" >&2
    exit 1
fi

# Keep the canonical deploy lock open in this outer updater from before any git
# mutation until the stability window and any rollback are finished. The small
# Python probe acquires flock on the inherited open-file description; because
# fd 9 remains open in this shell, that same lock survives after the probe exits.
mkdir -p "$(dirname "$DEPLOY_LOCK")"
exec 9>>"$DEPLOY_LOCK"
if ! CLIENTPLATFORM_DEPLOY_LOCK_FD=9 python3 - <<'PY'
import fcntl
import os
import sys

fd = int(os.environ["CLIENTPLATFORM_DEPLOY_LOCK_FD"])
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(75)
PY
then
    echo "CLIENTPLATFORM_UPDATE_FAILED:production_deploy_already_running" >&2
    exit 1
fi
export CLIENTPLATFORM_DEPLOY_LOCK_FD=9

mkdir -p "$ROOT"
cd "$ROOT"

if [ ! -d .git ]; then
    git init
fi
if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$REPOSITORY"
else
    git remote add origin "$REPOSITORY"
fi

git fetch --no-tags --prune --depth 1 origin "$TARGET_REF"
TARGET_SHA=$(git rev-parse FETCH_HEAD)
if [ -n "$EXPECTED_SHA" ] && [ "$TARGET_SHA" != "$EXPECTED_SHA" ]; then
    echo "CLIENTPLATFORM_UPDATE_FAILED:unexpected_target_sha" >&2
    exit 1
fi
git reset --hard "$TARGET_SHA"

if [ ! -f deploy/clientplatform/clientplatform.env ]; then
    echo "CLIENTPLATFORM_UPDATE_FAILED:clientplatform_env_missing" >&2
    exit 1
fi
chmod 600 deploy/clientplatform/clientplatform.env
[ ! -f deploy/clientplatform/.env ] || chmod 600 deploy/clientplatform/.env

DOMAIN=$(sed -n 's/^CLIENTPLATFORM_DOMAIN=//p' deploy/clientplatform/clientplatform.env | tail -n 1 | tr -d '\r')
DEPLOY_STARTED_EPOCH=$(date +%s)

# Direct operator/debug entrypoint remains:
# exec python3 -m scripts.clientplatform_production_deploy "$@"
# The updater intentionally does not exec it: the outer shell must retain fd 9
# through the post-deploy stability window and any rollback. The wrapper below
# calls that same deploy() implementation while sharing the inherited lock.
python3 -m scripts.clientplatform_locked_production_deploy "$@"
STABILITY_STARTED_EPOCH=$(date +%s)

post_deploy_ready() {
    docker exec "$APP_CONTAINER" python -c \
        "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8182/readyz',timeout=3)); raise SystemExit(0 if d.get('ok') is True else 1)" \
        >/dev/null 2>&1
}

post_deploy_crashed() {
    docker logs --since "$DEPLOY_STARTED_EPOCH" "$APP_CONTAINER" 2>&1 \
        | grep -Fq "Application crashed"
}

rollback_after_unstable_runtime() {
    reason=$1
    if [ ! -s "$EVIDENCE" ]; then
        echo "CLIENTPLATFORM_UPDATE_FAILED:${reason}:evidence_missing" >&2
        return 1
    fi

    python3 - "$EVIDENCE" "$DOMAIN" "$TARGET_SHA" "$reason" <<'PY'
import json
import sys

from scripts.clientplatform_production_deploy import (
    APP_CONTAINER,
    _completed_at,
    _compose,
    _ready,
    _rollback,
    _write_evidence,
)

evidence_path, domain, target_sha, reason = sys.argv[1:]
with open(evidence_path, encoding="utf-8") as handle:
    evidence = json.load(handle)
rollback_tag = str(evidence.get("rollback_tag") or "")
baseline_ready = evidence.get("baseline_ready") is True
recovery_mode = evidence.get("recovery_mode") is True

# Never restore a baseline that production_deploy explicitly classified as
# unavailable. A failed recovery deployment remains failed, but the known-bad
# old image is not resurrected merely because the post-deploy window failed.
if recovery_mode or not baseline_ready:
    path = _write_evidence(
        {
            "ok": False,
            "operation": "production_post_deploy_recovery_failed",
            "target_sha": target_sha,
            "rollback_tag": rollback_tag,
            "domain": domain,
            "failure_class": reason,
            "baseline_ready": baseline_ready,
            "recovery_mode": recovery_mode,
            "rollback_skipped": True,
            "app_container": APP_CONTAINER,
            "completed_at": _completed_at(),
        }
    )
    print(f"CLIENTPLATFORM_PRODUCTION_POST_DEPLOY_ROLLBACK_SKIPPED:{path}")
    raise SystemExit(0)

if not rollback_tag:
    print(
        f"CLIENTPLATFORM_UPDATE_FAILED:{reason}:rollback_tag_missing",
        file=sys.stderr,
    )
    raise SystemExit(1)

_rollback(
    compose=_compose(),
    rollback_tag=rollback_tag,
    domain=domain,
    timeout_seconds=120,
)
path = _write_evidence(
    {
        "ok": False,
        "operation": "production_post_deploy_rollback",
        "target_sha": target_sha,
        "rollback_tag": rollback_tag,
        "domain": domain,
        "failure_class": reason,
        "baseline_ready": baseline_ready,
        "recovery_mode": recovery_mode,
        "rollback_full_readiness": _ready(),
        "app_container": APP_CONTAINER,
        "completed_at": _completed_at(),
    }
)
print(f"CLIENTPLATFORM_PRODUCTION_POST_DEPLOY_ROLLBACK_OK:{path}")
PY
}

container_id=$(docker inspect --format '{{.Id}}' "$APP_CONTAINER" 2>/dev/null || true)
restart_count=$(docker inspect --format '{{.RestartCount}}' "$APP_CONTAINER" 2>/dev/null || true)
if [ -z "$container_id" ] || [ -z "$restart_count" ]; then
    rollback_after_unstable_runtime post_deploy_container_missing || true
    echo "CLIENTPLATFORM_UPDATE_FAILED:post_deploy_container_missing" >&2
    exit 1
fi

post_deploy_failure() {
    current_id=$(docker inspect --format '{{.Id}}' "$APP_CONTAINER" 2>/dev/null || true)
    current_status=$(docker inspect --format '{{.State.Status}}' "$APP_CONTAINER" 2>/dev/null || true)
    current_restarts=$(docker inspect --format '{{.RestartCount}}' "$APP_CONTAINER" 2>/dev/null || true)

    if [ "$current_id" != "$container_id" ]; then
        printf '%s\n' post_deploy_container_replaced
    elif [ "$current_status" != "running" ]; then
        printf '%s\n' post_deploy_container_not_running
    elif [ "$current_restarts" != "$restart_count" ]; then
        printf '%s\n' post_deploy_container_restarted
    elif post_deploy_crashed; then
        printf '%s\n' post_deploy_application_crashed
    elif ! post_deploy_ready; then
        printf '%s\n' post_deploy_readiness_lost
    else
        return 1
    fi
    return 0
}

deadline=$((STABILITY_STARTED_EPOCH + STABILITY_SECONDS))
while [ "$(date +%s)" -lt "$deadline" ]; do
    if failure=$(post_deploy_failure); then
        rollback_after_unstable_runtime "$failure" || true
        echo "CLIENTPLATFORM_UPDATE_FAILED:$failure" >&2
        exit 1
    fi
    sleep 2
done

# Re-run the complete identity/status/restart/crash/readiness invariant at the
# deadline. A container that restarted after the loop's last sample must not be
# accepted merely because Docker brought /readyz back before this final probe.
if failure=$(post_deploy_failure); then
    rollback_after_unstable_runtime "$failure" || true
    echo "CLIENTPLATFORM_UPDATE_FAILED:$failure" >&2
    exit 1
fi

echo "CLIENTPLATFORM_UPDATE_STABILITY_OK:${STABILITY_SECONDS}s"