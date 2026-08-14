#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Compatibility entrypoint only. The canonical production deploy implementation is
# owned by ClientPlatform itself so backup, readiness, rollback and app-state
# validation cannot drift from the running system.
exec python3 "$ROOT/scripts/clientplatform_production_deploy.py" "$@"
