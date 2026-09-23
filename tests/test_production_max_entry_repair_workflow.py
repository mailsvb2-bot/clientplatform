from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "production-max-entry-repair.yml"
RECOVERY = ROOT / "scripts" / "clientplatform_recover_max_production_env.py"


def test_production_max_entry_repair_is_manual_fail_closed_and_secret_safe() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    for required in (
        "workflow_dispatch:",
        "/opt/clientplatform",
        "sudo -n bash -s",
        "StrictHostKeyChecking=yes",
        "UserKnownHostsFile=",
        "CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY",
        "CLIENTPLATFORM_MAX_BOT_TOKEN",
        "CLIENTPLATFORM_MAX_REPAIR_TOKEN_STAGED",
        "CLIENTPLATFORM_MAX_REPAIR_TRUST_OK",
        "scripts/install_max_trust.sh",
        "/run/secrets/clientplatform-managed-bot/max-ca.pem",
        "russian_trusted_root_ca.crt",
        "russian_trusted_sub_ca.crt",
        "Remove temporary MAX token material",
        "MAX_BOT_TOKEN=",
        "clientplatform_recover_max_production_env.py",
        "scripts/register_max_webhook.py --apply",
        "scripts/max_provider_audit.py",
        "CLIENTPLATFORM_MAX_REPAIR_ROLLED_BACK",
        "CLIENTPLATFORM_MAX_REPAIR_CONFIG_OK",
        "CLIENTPLATFORM_MAX_OWNER_ENTRY_OK",
        "CLIENTPLATFORM_MAX_PROVIDER_OK",
        "CLIENTPLATFORM_MAX_REPAIR_OK",
        "CLIENTPLATFORM_MAX_PUBLIC_ENTRY_OK",
        "https://app.clientplatform.ru/clientplatform/open/max",
    ):
        assert required in text

    recovery = RECOVERY.read_text(encoding="utf-8")
    for required in (
        "MAX_BOT_TOKEN",
        "MAX_WEBHOOK_SECRET",
        "MAX_BOT_LINK_BASE",
        "MAX_BOT_NAME",
        "MAX_WEBHOOK_ENABLED",
        "https://platform-api2.max.ru",
        'env_file.name + "."',
        "max_bot_token_not_recoverable",
        "multiple_valid_max_bot_identities",
    ):
        assert required in recovery

    for forbidden in (
        "printenv",
        "set -x",
        'cat "$env_file"',
        'echo "$MAX_BOT_TOKEN_FALLBACK"',
        'printf "$MAX_BOT_TOKEN_FALLBACK"',
        "docker system prune",
        "docker volume prune",
        "git reset --hard",
        "StrictHostKeyChecking=no",
    ):
        assert forbidden not in text
