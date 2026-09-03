from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import clientplatform_production_deploy as deploy


def test_host_only_deploy_keeps_safety_gates_without_runtime_recreate(monkeypatch) -> None:
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
        "mode": "host_only_noop",
        "previous_successful_deploy_sha": "c" * 40,
        "changed_files": ["scripts/clientplatform_production_deploy.py"],
        "reason": "host_only_diff_proven",
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
    evidence: dict[str, object] = {}
    events: list[str] = []

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
    monkeypatch.setattr(
        deploy,
        "_wait_for_baseline_readiness",
        lambda _: events.append("baseline"),
    )
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
        lambda: {"removed": ["stale"], "removed_count": 1, "protected_count": 2, "foreign_skipped_count": 0},
    )
    monkeypatch.setattr(
        deploy,
        "_prune_deploy_image_history",
        lambda _: {"removed_tags": 0, "app_rollbacks_retained_before_deploy": 1, "visual_rollbacks_retained_before_deploy": 1},
    )
    monkeypatch.setattr(deploy, "_remove_transient_backup_image", lambda: {"present": False, "removed": False})
    monkeypatch.setattr(deploy, "_prune_build_cache_for_capacity", lambda **_: cache)
    monkeypatch.setattr(deploy, "_encrypted_backup", lambda _: "/backup/proof.dump.age")
    monkeypatch.setattr(
        deploy,
        "_cleanup_after_encrypted_backup",
        lambda: {"transient_backup_image": {"removed": True}, "build_cache_retention": cache, "disk_before_cleanup": capacity, "disk_after_cleanup": capacity, "capacity_ready": True},
    )
    monkeypatch.setattr(deploy, "_wait_for_visual_gateway", lambda _: events.append("gateway"))
    monkeypatch.setattr(deploy, "_external_https", lambda _: events.append("https"))
    monkeypatch.setattr(deploy, "_sales_operations_smoke", lambda: sales)
    monkeypatch.setattr(deploy, "_post_deploy_retention", lambda _: post_retention)
    monkeypatch.setattr(deploy, "_wait_for_readiness", lambda _: events.append("runtime"))

    def write_evidence(payload: dict[str, object]) -> Path:
        evidence.update(payload)
        return Path("/evidence/host-only.json")

    monkeypatch.setattr(deploy, "_write_evidence", write_evidence)
    result = deploy.deploy(allow_local_backup=False, timeout_seconds=240)

    assert result == Path("/evidence/host-only.json")
    assert events == ["baseline", "root", "gateway", "baseline", "https"]
    assert not any("build" in command for command in commands)
    assert not any("--force-recreate" in command for command in commands)
    assert [*compose, "up", "-d", "postgres"] in commands
    assert evidence["runtime_rollout_mode"] == "host_only_noop"
    assert evidence["change_contract"] == contract
    assert evidence["predeploy_stale_image_cleanup"]["removed_count"] == 1
    assert evidence["backup_mode"] == "encrypted"
    assert evidence["backup_reference"] == "/backup/proof.dump.age"
    assert evidence["sales_operations_smoke"] == sales
    assert evidence["post_deploy_retention"]["capacity_ready"] is True
