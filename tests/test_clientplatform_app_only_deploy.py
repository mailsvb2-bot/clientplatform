from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import clientplatform_production_deploy as deploy


def test_change_contract_proves_app_only_and_fails_closed_for_visual_changes(monkeypatch) -> None:
    previous_sha = "c" * 40
    target_sha = "d" * 40
    monkeypatch.setattr(deploy, "_latest_successful_deploy_sha", lambda: previous_sha)
    monkeypatch.setattr(deploy, "_git_is_ancestor", lambda ancestor, descendant: True)

    app_only_files = (
        ".github/workflows/production-disk-maintenance.yml",
        "clientplatform/application/native_member_interactions.py",
        "clientplatform/application/owner_input.py",
        "clientplatform/infrastructure/owner_input_repository.py",
        "docs/production-deploys/proof.md",
        "tests/test_app_only.py",
        "scripts/clientplatform_production_deploy.py",
    )
    monkeypatch.setattr(deploy, "_changed_files_between", lambda *_: app_only_files)
    contract = deploy._deployment_change_contract(target_sha, baseline_ready=True)
    assert contract == {
        "mode": "app_only",
        "previous_successful_deploy_sha": previous_sha,
        "changed_files": list(app_only_files),
        "reason": "app_only_diff_proven",
    }

    visual_files = (*app_only_files, "visual_gateway/server.py")
    monkeypatch.setattr(deploy, "_changed_files_between", lambda *_: visual_files)
    contract = deploy._deployment_change_contract(target_sha, baseline_ready=True)
    assert contract["mode"] == "full_runtime"
    assert contract["reason"] == "runtime_paths_changed"

    host_only_files = (
        ".github/workflows/production-disk-maintenance.yml",
        "docs/production-deploys/proof.md",
        "tests/test_app_only.py",
        "scripts/clientplatform_production_deploy.py",
    )
    monkeypatch.setattr(deploy, "_changed_files_between", lambda *_: host_only_files)
    contract = deploy._deployment_change_contract(target_sha, baseline_ready=True)
    assert contract["mode"] == "host_only_noop"
    assert contract["reason"] == "host_only_diff_proven"


def test_app_only_keeps_full_runtime_disk_thresholds() -> None:
    assert deploy._disk_capacity_limits("app_only") == deploy._disk_capacity_limits("full_runtime")




def test_app_only_rollback_does_not_touch_visual_gateway(monkeypatch) -> None:
    compose = ["docker", "compose", "--env-file", "clientplatform.env"]
    commands: list[list[str]] = []
    restored_visual: list[str] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deploy, "_run", run)
    monkeypatch.setattr(
        deploy,
        "_restore_visual_gateway",
        lambda **kwargs: restored_visual.append(str(kwargs["rollback_tag"])),
    )
    monkeypatch.setattr(deploy, "_wait_for_baseline_readiness", lambda _: None)
    monkeypatch.setattr(deploy, "_external_https", lambda _: None)

    deploy._rollback(
        compose=compose,
        rollback_tag=f"{deploy.APP_IMAGE}:rollback-proof",
        visual_gateway_rollback_tag="",
        domain="clientplatform.example.test",
        timeout_seconds=60,
    )

    assert restored_visual == []
    assert commands == [
        [
            "docker",
            "image",
            "tag",
            f"{deploy.APP_IMAGE}:rollback-proof",
            f"{deploy.APP_IMAGE}:latest",
        ],
        [
            *compose,
            "up",
            "-d",
            "--no-build",
            "--force-recreate",
            "app",
            "caddy",
        ],
    ]


def test_app_only_deploy_rebuilds_app_without_rebuilding_visual_gateway(monkeypatch) -> None:
    compose = ["docker", "compose", "--env-file", "clientplatform.env"]
    app_image = "sha256:" + "a" * 64
    visual_image = "sha256:" + "b" * 64
    target_sha = "d" * 40
    capacity = {
        "total_bytes": 30 * 1024**3,
        "used_bytes": 18 * 1024**3,
        "free_bytes": 12 * 1024**3,
        "used_percent": 60.0,
    }
    cache = {
        "mode": "bounded",
        "keep_storage": "2GB",
        "before_cleanup": capacity,
        "after_cleanup": capacity,
        "pressure_cleanup_applied": False,
    }
    contract = {
        "mode": "app_only",
        "previous_successful_deploy_sha": "c" * 40,
        "changed_files": ["clientplatform/application/native_member_interactions.py"],
        "reason": "app_only_diff_proven",
    }
    post_retention = {
        "image_retention": {"removed_tags": 0},
        "transient_backup_image": {"removed": False},
        "build_cache_retention": cache,
        "disk_before_cleanup": capacity,
        "disk_after_cleanup": capacity,
        "capacity_ready": True,
    }
    sales = {
        "contract_version": "u008-u009-sales-operations-v2",
        "ok": True,
        "rollback_clean": True,
        "checks": {name: True for name in deploy._SALES_SMOKE_REQUIRED_CHECKS},
        "residue": {"businesses": 0},
    }
    commands: list[list[str]] = []
    events: list[str] = []
    evidence: dict[str, object] = {}

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deploy.os, "geteuid", lambda: 0)
    monkeypatch.setattr(deploy, "_assert_tracked_worktree_clean", lambda: None)
    monkeypatch.setattr(deploy, "prepare", lambda _: ())
    monkeypatch.setattr(
        deploy,
        "_env_values",
        lambda _: {
            "CLIENTPLATFORM_DOMAIN": "clientplatform.example.test",
            "CLIENTPLATFORM_BACKUP_AGE_RECIPIENT": "age1test",
        },
    )
    monkeypatch.setattr(deploy, "_git_sha", lambda: target_sha)
    monkeypatch.setattr(deploy, "_compose", lambda: compose)
    monkeypatch.setattr(deploy, "_run", run)
    monkeypatch.setattr(deploy, "_container_exists", lambda _: True)
    monkeypatch.setattr(deploy, "_wait_for_baseline_readiness", lambda _: events.append("baseline"))
    monkeypatch.setattr(deploy, "_external_root", lambda _: events.append("root"))
    monkeypatch.setattr(deploy, "_deployment_change_contract", lambda *_args, **_kwargs: contract)
    monkeypatch.setattr(
        deploy,
        "_container_image",
        lambda container: app_image if container == deploy.APP_CONTAINER else visual_image,
    )
    monkeypatch.setattr(deploy, "_optional_container_image", lambda _: visual_image)
    monkeypatch.setattr(
        deploy,
        "_cleanup_stale_project_images",
        lambda: {
            "removed": [],
            "removed_count": 0,
            "protected_count": 2,
            "foreign_skipped_count": 0,
        },
    )
    monkeypatch.setattr(
        deploy,
        "_prune_deploy_image_history",
        lambda _: {
            "removed_tags": 0,
            "app_rollbacks_retained_before_deploy": 1,
            "visual_rollbacks_retained_before_deploy": 1,
        },
    )
    monkeypatch.setattr(deploy, "_remove_transient_backup_image", lambda: {"present": False, "removed": False})
    monkeypatch.setattr(deploy, "_prune_images_without_containers", lambda: {"pruned": True})
    monkeypatch.setattr(deploy, "_prune_build_cache_for_capacity", lambda **_: cache)
    monkeypatch.setattr(deploy, "_encrypted_backup", lambda _: "/backup/proof.dump.age")
    monkeypatch.setattr(
        deploy,
        "_cleanup_after_encrypted_backup",
        lambda **_: {
            "transient_backup_image": {"removed": True},
            "build_cache_retention": cache,
            "disk_before_cleanup": capacity,
            "disk_after_cleanup": capacity,
            "capacity_ready": True,
        },
    )
    monkeypatch.setattr(deploy, "_wait_for_visual_gateway", lambda _: events.append("gateway"))
    monkeypatch.setattr(deploy, "_wait_for_readiness", lambda _: events.append("runtime"))
    monkeypatch.setattr(deploy, "_external_https", lambda _: events.append("https"))
    monkeypatch.setattr(deploy, "_sales_operations_smoke", lambda: sales)
    monkeypatch.setattr(deploy, "_post_deploy_retention", lambda _, **__: post_retention)

    def write_evidence(payload: dict[str, object]) -> Path:
        evidence.update(payload)
        return Path("/evidence/app-only.json")

    monkeypatch.setattr(deploy, "_write_evidence", write_evidence)

    result = deploy.deploy(allow_local_backup=False, timeout_seconds=240)

    assert result == Path("/evidence/app-only.json")
    assert [*compose, "build", "app"] in commands
    assert [*compose, "build", "visual-gateway"] not in commands
    assert [
        *compose,
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "app",
        "caddy",
    ] in commands
    assert [*compose, "up", "-d", "--force-recreate", "app", "caddy"] not in commands
    assert [*compose, "up", "-d", "--force-recreate", "visual-gateway"] not in commands
    assert events == ["baseline", "root", "gateway", "runtime", "https"]
    visual_release = f"{deploy.VISUAL_GATEWAY_IMAGE}:release-{target_sha}"
    assert not any(visual_release in command for command in commands)
    visual_rollback_prefix = f"{deploy.VISUAL_GATEWAY_IMAGE}:rollback-"
    assert not any(
        any(part.startswith(visual_rollback_prefix) for part in command)
        for command in commands
    )
    assert evidence["runtime_rollout_mode"] == "app_only"
    assert evidence["change_contract"] == contract
    assert evidence["post_deploy_retention"]["capacity_ready"] is True
