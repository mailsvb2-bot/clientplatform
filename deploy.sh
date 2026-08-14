#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Canonical production entrypoint. Provider-adapter rollout is staged and health-
# gated before the existing app/gateway deploy core performs backup, readiness and
# rollback. The legacy standalone provider container is not removed by this step.
exec python3 "$ROOT/scripts/clientplatform_visual_provider_rollout.py" "$@"
