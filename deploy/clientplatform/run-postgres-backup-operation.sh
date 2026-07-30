#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

DEPLOY_DIR="${CLIENTPLATFORM_COMPOSE_DIR:-/opt/clientplatform/deploy/clientplatform}"
ACTION="${1:-backup}"

cd "$DEPLOY_DIR"

test -f compose.production.yml
test -f clientplatform.env

compose=(docker compose)
if [[ -f .env ]]; then
  compose+=(--env-file .env)
fi
compose+=(--env-file clientplatform.env -f compose.production.yml --profile operations)

case "$ACTION" in
  backup)
    exec "${compose[@]}" run --rm backup
    ;;
  freshness)
    exec "${compose[@]}" run --rm --no-deps \
      --entrypoint python backup \
      -m scripts.clientplatform_postgres_backup_freshness
    ;;
  *)
    echo "Unsupported ClientPlatform backup operation" >&2
    exit 64
    ;;
esac
