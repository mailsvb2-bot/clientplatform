from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from scripts import clientplatform_production_deploy as production_deploy


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts/clientplatform_production_deploy.py"


def test_direct_deploy_script_help_works_without_pythonpath() -> None:
    if os.name != "posix":
        pytest.skip("production deploy entrypoint is POSIX-only")

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(DEPLOY_SCRIPT), "--help"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--timeout-seconds" in completed.stdout
    assert "ModuleNotFoundError" not in completed.stderr


def test_tracked_worktree_guard_accepts_clean_repository() -> None:
    completed = subprocess.CompletedProcess(
        args=["git", "status"],
        returncode=0,
        stdout="",
        stderr="",
    )
    with mock.patch.object(production_deploy, "_run", return_value=completed) as run:
        production_deploy._assert_tracked_worktree_clean()

    run.assert_called_once_with(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
            "--ignore-submodules=none",
        ],
        cwd=production_deploy.ROOT,
        capture=True,
    )


def test_tracked_worktree_guard_rejects_modified_tracked_file() -> None:
    completed = subprocess.CompletedProcess(
        args=["git", "status"],
        returncode=0,
        stdout=" M deploy/clientplatform/configure-backup-age.sh\n",
        stderr="",
    )
    with mock.patch.object(production_deploy, "_run", return_value=completed):
        with pytest.raises(
            production_deploy.DeploymentError,
            match="tracked_worktree_dirty",
        ):
            production_deploy._assert_tracked_worktree_clean()


def test_deploy_checks_tracked_source_before_environment_mutation() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    deploy_source = source[source.index("def deploy(") :]

    assert deploy_source.index("_assert_tracked_worktree_clean()") < deploy_source.index(
        'env_file = DEPLOY_DIR / "clientplatform.env"'
    )
    assert deploy_source.index("_assert_tracked_worktree_clean()") < deploy_source.index(
        "prepare(env_file)"
    )
