#!/bin/sh
set -eu

ROOT=${CLIENTPLATFORM_ROOT:-/opt/clientplatform}
REPOSITORY=${CLIENTPLATFORM_REPOSITORY:-https://github.com/mailsvb2-bot/clientplatform.git}
TARGET_REF=${CLIENTPLATFORM_TARGET_REF:-main}
EXPECTED_SHA=${CLIENTPLATFORM_EXPECTED_SHA:-}

if [ "$(id -u)" -ne 0 ]; then
    echo "CLIENTPLATFORM_UPDATE_FAILED:root_required" >&2
    exit 1
fi

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

exec python3 scripts/clientplatform_production_deploy.py "$@"
