from __future__ import annotations

import fcntl
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

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


class ClientPlatformUpdateProductionStabilityTests(unittest.TestCase):
    def test_update_production_shell_is_syntactically_valid(self) -> None:
        subprocess.run(["sh", "-n", str(_script())], check=True)

    def test_update_production_waits_for_stability_and_fails_closed(self) -> None:
        source = _script().read_text(encoding="utf-8")

        self.assertIn(
            "DEPLOY_LOCK=/run/lock/clientplatform-production-deploy.lock",
            source,
        )
        self.assertIn('exec 9>>"$DEPLOY_LOCK"', source)
        self.assertIn("CLIENTPLATFORM_DEPLOY_LOCK_FD=9", source)
        self.assertIn(
            'python3 -m scripts.clientplatform_locked_production_deploy "$@"',
            source,
        )
        self.assertIn("CLIENTPLATFORM_UPDATE_STABILITY_OK", source)
        self.assertIn("post_deploy_failure()", source)
        self.assertGreaterEqual(source.count("post_deploy_failure"), 3)
        self.assertIn("post_deploy_application_crashed", source)
        self.assertIn("post_deploy_container_restarted", source)
        self.assertIn("post_deploy_container_replaced", source)
        self.assertIn("post_deploy_container_not_running", source)
        self.assertIn("post_deploy_readiness_lost", source)
        self.assertIn("production_post_deploy_rollback", source)
        self.assertIn("production_post_deploy_recovery_failed", source)
        self.assertIn('evidence.get("baseline_ready") is True', source)
        self.assertIn('evidence.get("recovery_mode") is True', source)
        self.assertIn('"rollback_skipped": True', source)
        self.assertIn(
            "CLIENTPLATFORM_PRODUCTION_POST_DEPLOY_ROLLBACK_OK",
            source,
        )
        self.assertIn(
            "CLIENTPLATFORM_PRODUCTION_POST_DEPLOY_ROLLBACK_SKIPPED",
            source,
        )

    def test_inherited_lock_primitive_blocks_second_open_file_description(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "production-deploy.lock"
            lock_path.touch()
            inherited_fd = os.open(lock_path, os.O_RDWR | os.O_APPEND)
            contender_fd = os.open(lock_path, os.O_RDWR | os.O_APPEND)
            try:
                acquire_inherited_deploy_lock(inherited_fd, lock_path=lock_path)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(inherited_fd)
            try:
                fcntl.flock(contender_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(contender_fd)


if __name__ == "__main__":
    unittest.main()
