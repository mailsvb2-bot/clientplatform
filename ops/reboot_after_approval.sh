#!/usr/bin/env bash
set -Eeuo pipefail

# Reboots only with an explicit approved-window marker.
# Example:
#   APPROVED_REBOOT_WINDOW="2026-07-09 03:00 MSK" ops/reboot_after_approval.sh

if [ -z "${APPROVED_REBOOT_WINDOW:-}" ]; then
  echo "REFUSED: set APPROVED_REBOOT_WINDOW to the agreed maintenance window before rebooting."
  exit 2
fi

echo "=== approved reboot requested: $(date -Is) ==="
echo "=== approved window: ${APPROVED_REBOOT_WINDOW} ==="
echo "=== pre-reboot health ==="
curl -fsS http://127.0.0.1:8082/healthz || true
echo

echo "=== services before reboot ==="
systemctl is-active clientplatform.service || true
systemctl is-active github-deploy-webhook.service || true

echo "=== rebooting now ==="
systemctl reboot
