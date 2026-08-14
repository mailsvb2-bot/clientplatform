from __future__ import annotations

from pathlib import Path

from scripts import clientplatform_production_deploy as deploy

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "clientplatform" / "compose.production.yml"
RECOVERY_WORKFLOW = ROOT / ".github" / "workflows" / "production-deploy-recovery.yml"
DEPLOY_SCRIPT = ROOT / "scripts" / "clientplatform_production_deploy.py"


def _service_block(text: str, service: str, next_service: str) -> str:
    return text.split(f"  {service}:\n", 1)[1].split(f"\n  {next_service}:\n", 1)[0]


def test_visual_gateway_is_internal_versioned_production_service() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    gateway = _service_block(text, "visual-gateway", "app")
    app = _service_block(text, "app", "caddy")

    assert "dockerfile: visual_gateway/Dockerfile" in gateway
    assert "image: clientplatform-production-visual-gateway" in gateway
    assert "VISUAL_GATEWAY_UPSTREAM_URL: ${VISUAL_GATEWAY_URL:?set VISUAL_GATEWAY_URL}" in gateway
    assert "VISUAL_GATEWAY_UPSTREAM_TOKEN: ${VISUAL_GATEWAY_TOKEN:?set VISUAL_GATEWAY_TOKEN}" in gateway
    assert "VISUAL_GATEWAY_STATE_DIR: /var/lib/visual-gateway" in gateway
    assert "clientplatform-visual-gateway:/var/lib/visual-gateway" in gateway
    assert 'expose: ["8080"]' in gateway
    assert "ports:" not in gateway
    assert "/v1/capabilities" in gateway
    assert "Authorization':'Bearer '+token" in gateway
    assert "'contract_version':'1.0'" in gateway

    assert "VISUAL_GATEWAY_URL: http://visual-gateway:8080" in app
    assert "visual-gateway:" in app
    assert "condition: service_healthy" in app
    assert "clientplatform-visual-gateway:" in text


def test_recovery_workflow_uses_single_canonical_deploy_entrypoint() -> None:
    workflow = RECOVERY_WORKFLOW.read_text(encoding="utf-8")

    assert "python3 scripts/clientplatform_production_deploy.py" in workflow
    assert "clientplatform_production_deploy_with_visual_gateway.py" not in workflow


def test_canonical_deploy_orders_gateway_before_app_and_versions_release() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    deploy_body = source.split("\ndef deploy(\n", 1)[1].split("\ndef main()", 1)[0]

    build = deploy_body.index('"build", "visual-gateway", "app", "backup"')
    gateway_up = deploy_body.index('"--force-recreate", "visual-gateway"')
    gateway_ready = deploy_body.index("_wait_for_visual_gateway(timeout_seconds)", gateway_up)
    app_up = deploy_body.index('"--force-recreate", "app", "caddy"', gateway_ready)
    app_ready = deploy_body.index("_wait_for_readiness(timeout_seconds)", app_up)

    assert build < gateway_up < gateway_ready < app_up < app_ready
    assert 'f"{VISUAL_GATEWAY_IMAGE}:release-{target_sha}"' in deploy_body
    assert '"visual_gateway_contract_version": "1.0"' in deploy_body


def test_restore_visual_gateway_uses_previous_image_before_recreate(monkeypatch) -> None:
    calls: list[list[str]] = []
    waits: list[int] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy, "_wait_for_visual_gateway", waits.append)

    deploy._restore_visual_gateway(
        compose=["docker", "compose", "-f", "compose.production.yml"],
        rollback_tag="clientplatform-production-visual-gateway:rollback-test",
        timeout_seconds=77,
    )

    assert calls[0] == [
        "docker",
        "image",
        "tag",
        "clientplatform-production-visual-gateway:rollback-test",
        "clientplatform-production-visual-gateway:latest",
    ]
    assert calls[1][-4:] == [
        "-d",
        "--no-build",
        "--force-recreate",
        "visual-gateway",
    ]
    assert waits == [77]


def test_first_rollout_gateway_restore_keeps_healthy_gateway(monkeypatch) -> None:
    calls: list[list[str]] = []
    waits: list[int] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy, "_wait_for_visual_gateway", waits.append)

    deploy._restore_visual_gateway(
        compose=["docker", "compose"],
        rollback_tag="",
        timeout_seconds=91,
    )

    assert calls == []
    assert waits == [91]


def test_first_rollout_gateway_cleanup_does_not_delete_state_volume(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(deploy, "_run", fake_run)

    deploy._remove_first_rollout_visual_gateway(["docker", "compose"])

    assert calls == [["docker", "compose", "rm", "--force", "--stop", "visual-gateway"]]
    assert "--volumes" not in calls[0]
