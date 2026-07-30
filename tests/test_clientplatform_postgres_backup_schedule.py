from __future__ import annotations

import unittest
from pathlib import Path


class ClientPlatformPostgresBackupScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.deploy = cls.root / "deploy/clientplatform"

    def test_backup_timer_is_hourly_persistent_and_jittered(self) -> None:
        timer = (self.deploy / "clientplatform-postgres-backup.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn("OnCalendar=hourly", timer)
        self.assertIn("RandomizedDelaySec=5min", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=clientplatform-postgres-backup.service", timer)

    def test_freshness_timer_is_persistent_and_runs_every_fifteen_minutes(self) -> None:
        timer = (
            self.deploy / "clientplatform-postgres-backup-freshness.timer"
        ).read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*:05/15", timer)
        self.assertIn("RandomizedDelaySec=60", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn(
            "Unit=clientplatform-postgres-backup-freshness.service",
            timer,
        )

    def test_services_share_one_lock_and_do_not_restart_the_application(self) -> None:
        backup = (
            self.deploy / "clientplatform-postgres-backup.service"
        ).read_text(encoding="utf-8")
        freshness = (
            self.deploy / "clientplatform-postgres-backup-freshness.service"
        ).read_text(encoding="utf-8")
        lock = "/run/lock/clientplatform-postgres-backup.lock"
        self.assertIn(lock, backup)
        self.assertIn(lock, freshness)
        self.assertIn("run-postgres-backup-operation.sh backup", backup)
        self.assertIn("run-postgres-backup-operation.sh freshness", freshness)
        self.assertNotIn("Restart=", backup)
        self.assertNotIn("Restart=", freshness)
        self.assertNotIn("clientplatform-production-app-1", backup)
        self.assertNotIn("clientplatform-production-app-1", freshness)

    def test_services_require_docker_and_use_bounded_resources(self) -> None:
        for name in (
            "clientplatform-postgres-backup.service",
            "clientplatform-postgres-backup-freshness.service",
        ):
            with self.subTest(name=name):
                unit = (self.deploy / name).read_text(encoding="utf-8")
                self.assertIn("Wants=network-online.target docker.service", unit)
                self.assertIn("After=network-online.target docker.service", unit)
                self.assertIn("User=root", unit)
                self.assertIn("NoNewPrivileges=true", unit)
                self.assertIn("ProtectSystem=strict", unit)
                self.assertIn("ProtectHome=true", unit)
                self.assertIn("RestrictAddressFamilies=AF_UNIX", unit)
                self.assertIn("UMask=0077", unit)
                self.assertIn("TimeoutStartSec=", unit)

    def test_operation_runner_uses_the_compose_operations_profile(self) -> None:
        runner = (self.deploy / "run-postgres-backup-operation.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("set -Eeuo pipefail", runner)
        self.assertIn("umask 077", runner)
        self.assertIn("--profile operations", runner)
        self.assertIn("--env-file clientplatform.env", runner)
        self.assertIn("if [[ -f .env ]]", runner)
        self.assertIn('run --rm backup', runner)
        self.assertIn('run --rm --no-deps', runner)
        self.assertIn('-m scripts.clientplatform_postgres_backup_freshness', runner)
        self.assertIn("exit 64", runner)

    def test_compose_backup_service_runs_the_encrypted_pipeline(self) -> None:
        compose = (self.deploy / "compose.production.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('profiles: ["operations"]', compose)
        self.assertIn(
            'entrypoint: ["python", "-m", "scripts.clientplatform_postgres_backup_pipeline"]',
            compose,
        )
        self.assertIn("clientplatform-backups:/var/backups/clientplatform", compose)
        self.assertIn("clientplatform-state:/var/lib/clientplatform", compose)

    def test_schedule_is_opt_in_until_live_offsite_proof(self) -> None:
        env_example = (
            self.deploy / "clientplatform.production.env.example"
        ).read_text(encoding="utf-8")
        self.assertIn("CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED=0", env_example)
        self.assertIn(
            "CLIENTPLATFORM_POSTGRES_BACKUP_FRESHNESS_REQUIRED=0",
            env_example,
        )
        self.assertIn(
            "CLIENTPLATFORM_POSTGRES_BACKUP_MAX_AGE_SECONDS=10800",
            env_example,
        )


if __name__ == "__main__":
    unittest.main()
