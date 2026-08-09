from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

import pytest

from scripts.clientplatform_locked_production_deploy import (
    acquire_inherited_deploy_lock,
)


def _script() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "clientplatform"
        / "update-production.sh"
    )


def test_update_production_shell_is_syntactically_valid() -> None:
    subprocess.run(["sh", "-n", str(_script())], check=True)


def test_update_production_waits_for_stability_and_fails_closed() -> None:
    source = _script().read_text(encoding="utf-8")

    assert "DEPLOY_LOCK=/run/lock/clientplatform-production-deploy.lock" in source
    assert 'exec 9>>"$DEPLOY_LOCK"' in source
    assert "CLIENTPLATFORM_DEPLOY_LOCK_FD=9" in source
    assert 'python3 -m scripts.clientplatform_locked_production_deploy "$@"' in source
    assert "CLIENTPLATFORM_UPDATE_STABILITY_OK" in source
    assert "post_deploy_failure()" in source
    assert source.count("post_deploy_failure") >= 3
    assert "post_deploy_application_crashed" in source
    assert "post_deploy_container_restarted" in source
    assert "post_deploy_container_replaced" in source
    assert "post_deploy_container_not_running" in source
    assert "post_deploy_readiness_lost" in source
    assert "production_post_deploy_rollback" in source
    assert "production_post_deploy_recovery_failed" in source
    assert 'evidence.get("baseline_ready") is True' in source
    assert 'evidence.get("recovery_mode") is True' in source
    assert '"rollback_skipped": True' in source
    assert "CLIENTPLATFORM_PRODUCTION_POST_DEPLOY_ROLLBACK_OK" in source
    assert "CLIENTPLATFORM_PRODUCTION_POST_DEPLOY_ROLLBACK_SKIPPED" in source


def test_inherited_lock_primitive_blocks_a_second_open_file_description(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "production-deploy.lock"
    lock_path.touch()
    inherited_fd = os.open(lock_path, os.O_RDWR | os.O_APPEND)
    contender_fd = os.open(lock_path, os.O_RDWR | os.O_APPEND)
    try:
        acquire_inherited_deploy_lock(inherited_fd, lock_path=lock_path)
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(inherited_fd)
    try:
        fcntl.flock(contender_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(contender_fd)
