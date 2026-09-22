from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "production-max-entry-repair.yml"


def test_production_max_entry_repair_is_manual_fail_closed_and_secret_safe() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    for required in (
        "workflow_dispatch:",
        "/opt/clientplatform",
        "sudo -n bash -s",
        "StrictHostKeyChecking=yes",
        "UserKnownHostsFile=",
        "CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY",
        "MAX_BOT_TOKEN",
        "MAX_WEBHOOK_SECRET",
        "MAX_BOT_LINK_BASE",
        "MAX_WEBHOOK_ENABLED=1",
        "MAX_API_BASE_URL_OFFICIAL",
        "MAX_BOT_LINK_BASE_OFFICIAL_HTTPS",
        "provider_precheck",
        "scripts/register_max_webhook.py --apply",
        "scripts/max_provider_audit.py",
        "CLIENTPLATFORM_MAX_REPAIR_ROLLED_BACK",
        "CLIENTPLATFORM_MAX_OWNER_ENTRY_OK",
        "CLIENTPLATFORM_MAX_PROVIDER_OK",
        "CLIENTPLATFORM_MAX_PUBLIC_ENTRY_OK",
        "https://app.clientplatform.ru/clientplatform/open/max",
    ):
        assert required in text

    for forbidden in (
        "printenv",
        "set -x",
        'cat "$env_file"',
        "docker system prune",
        "docker volume prune",
        "git reset --hard",
        "StrictHostKeyChecking=no",
    ):
        assert forbidden not in text
