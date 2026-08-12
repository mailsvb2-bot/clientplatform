#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Compatibility entrypoint only. ClientPlatform production has exactly one deploy
# implementation; all locking, backup, readiness and rollback logic lives there.
exec python3 "$ROOT/scripts/clientplatform_production_deploy.py" "$@"
