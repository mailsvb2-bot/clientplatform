from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "deploy/clientplatform/OPERATIONS.md"
DEPLOY_SCRIPT = ROOT / "scripts/clientplatform_production_deploy.py"
COMPOSE_FILE = ROOT / "deploy/clientplatform/compose.production.yml"


def _bash_block_list(text: str) -> list[str]:
    return [
        match.group(1)
        for match in re.finditer(r"```bash\n(.*?)\n```", text, flags=re.DOTALL)
    ]


def _bash_blocks(text: str) -> str:
    return "\n".join(_bash_block_list(text))


def test_runbook_uses_only_real_deploy_cli_options() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    blocks = _bash_block_list(runbook)
    commands = "\n".join(blocks)
    deploy_commands = "\n".join(
        block
        for block in blocks
        if "clientplatform_production_deploy.py" in block
    )
    deploy_source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    for supported in (
        "--allow-local-backup",
        "--recover-unavailable-baseline",
        "--timeout-seconds",
    ):
        assert supported in deploy_source
        assert supported in runbook

    for unsupported in (
        "--ref",
        "--repository-root",
        "--deploy-root",
        "--env-file",
        "--compose-file",
        "--project-name",
    ):
        assert unsupported not in deploy_commands

    assert "clientplatform.env.example" not in runbook
    expected_default = (
        "clientplatform_production_deploy.py \\" + "\n  --timeout-seconds 240"
    )
    assert expected_default in commands


def test_runbook_matches_compose_services_and_internal_ports() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    commands = _bash_blocks(runbook)
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    for service in ("postgres", "app", "caddy", "backup", "s3-replication"):
        assert re.search(rf"^  {re.escape(service)}:\s*$", compose, flags=re.MULTILINE)
        assert f"`{service}`" in runbook

    assert "logs --tail=200 app caddy postgres" in commands
    assert "bot_gateway" not in commands
    assert ":8080" not in commands
    assert "127.0.0.1:8182/healthz" in commands
    assert "127.0.0.1:8182/readyz" in commands
    assert "8181" in runbook
    assert "8182" in runbook
    assert "8191" in runbook


def test_runbook_preserves_polling_only_public_contract() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    commands = _bash_blocks(runbook)

    assert "TELEGRAM_TRANSPORT must be polling" not in commands
    assert "${PREFIX:-/telegram-webhook}" in commands
    assert "X-Telegram-Bot-Api-Secret-Token: intentionally-invalid-operator-proof" in commands
    assert 'test "${STATUS}" = "404"' in commands
    assert '"https://${DOMAIN}/healthz"' in commands
    assert '"https://${DOMAIN}/readyz"' in commands
    assert commands.count('= "404"') >= 3
    assert not re.search(r"/health(?:[\"'\s]|$)", commands)


def test_runbook_orders_prepare_preflight_deploy_and_evidence() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    prepare_position = runbook.index("clientplatform_prepare_production_env.py")
    preflight_position = runbook.index("clientplatform_production_preflight.py")
    deploy_position = runbook.index("clientplatform_production_deploy.py")
    evidence_position = runbook.index(
        "/var/lib/clientplatform/deploy-evidence/latest.json"
    )

    assert prepare_position < preflight_position < deploy_position < evidence_position
    assert "--env-file deploy/clientplatform/clientplatform.env" in runbook
    assert "CLIENTPLATFORM_PRODUCTION_PREFLIGHT_OK" in runbook
    assert "CLIENTPLATFORM_PRODUCTION_DEPLOY_OK:<evidence-path>" in runbook
    assert '"telegram_webhook_absent": true' in runbook
    assert "старые rollback/release Docker-теги" in runbook
    assert "legacy `recovered-*`" in runbook
    assert "использовании от 75%" in runbook
    assert "свободном месте менее 7 GiB" in runbook
    assert "два последних rollback-поколения" in runbook
