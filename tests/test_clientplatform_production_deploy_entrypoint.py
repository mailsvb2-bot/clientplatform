from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from scripts import clientplatform_production_deploy as production_deploy


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts/clientplatform_production_deploy.py"


class ProductionDeployEntrypointTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "production deploy entrypoint is POSIX-only")
    def test_direct_deploy_script_help_works_without_pythonpath(self) -> None:
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

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--timeout-seconds", completed.stdout)
        self.assertNotIn("ModuleNotFoundError", completed.stderr)

    def test_tracked_worktree_guard_accepts_clean_repository(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with mock.patch.object(
            production_deploy.subprocess,
            "run",
            return_value=completed,
        ) as run:
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
            check=False,
            text=True,
            capture_output=True,
        )

    def test_tracked_worktree_guard_rejects_modified_tracked_file(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout=" M deploy/clientplatform/configure-backup-age.sh\n",
            stderr="",
        )
        with mock.patch.object(
            production_deploy.subprocess,
            "run",
            return_value=completed,
        ):
            with self.assertRaisesRegex(
                production_deploy.DeploymentError,
                "tracked_worktree_dirty",
            ):
                production_deploy._assert_tracked_worktree_clean()

    def test_tracked_worktree_guard_rejects_git_failure(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=128,
            stdout="",
            stderr="fatal: unavailable",
        )
        with mock.patch.object(
            production_deploy.subprocess,
            "run",
            return_value=completed,
        ):
            with self.assertRaisesRegex(
                production_deploy.DeploymentError,
                "tracked_worktree_check_failed",
            ):
                production_deploy._assert_tracked_worktree_clean()

    def test_deploy_checks_tracked_source_before_environment_mutation(self) -> None:
        source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        deploy_source = source[source.index("def deploy(") :]

        self.assertLess(
            deploy_source.index("_assert_tracked_worktree_clean()"),
            deploy_source.index('env_file = DEPLOY_DIR / "clientplatform.env"'),
        )
        self.assertLess(
            deploy_source.index("_assert_tracked_worktree_clean()"),
            deploy_source.index("prepare(env_file)"),
        )


if __name__ == "__main__":
    unittest.main()
