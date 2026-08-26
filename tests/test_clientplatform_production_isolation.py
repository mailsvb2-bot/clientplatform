from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.clientplatform_http_probe import _join, _load_replay_events, _percentile
from scripts.clientplatform_postgres_backup import (
    _database_name,
    _pg_environment,
    _safe_identifier,
)
from scripts.clientplatform_production_preflight import validate_environment


class ClientPlatformProductionIsolationTests(unittest.TestCase):
    def _valid_env(self) -> dict[str, str]:
        domain = "clientplatform.production.internal"
        return {
            "APP_ENV": "prod",
            "CLIENTPLATFORM_ENVIRONMENT": "production",
            "CLIENTPLATFORM_DEPLOYMENT_ID": "clientplatform-production",
            "CLIENTPLATFORM_DEPLOYMENT_MODE": "systemd",
            "CLIENTPLATFORM_CONTAINER_NETWORK_ISOLATED": "0",
            "CLIENTPLATFORM_DOMAIN": domain,
            "CLIENTPLATFORM_PUBLIC_BASE_URL": f"https://{domain}",
            "CLIENTPLATFORM_PRODUCTION_BOT_USERNAME": "clientplatform_bot",
            "BOT_TOKEN": "ci-bot-token-material-without-provider-shape",
            "ADMIN_IDS": "100001",
            "CLIENTPLATFORM_DB_ENGINE": "postgres",
            "CLIENTPLATFORM_DATABASE_NAME": "clientplatform_ci",
            "DATABASE_URL": (
                "postgresql://clientplatform_app:password@127.0.0.1:5433/"
                "clientplatform_ci"
            ),
            "ALLOW_SQLITE_IN_PROD": "0",
            "METRO_RUNTIME_ROOT": "/var/lib/clientplatform/runtime",
            "METRO_WRITABLE_ROOT": "/var/lib/clientplatform/state",
            "CLIENTPLATFORM_DATA_DIR": "/var/lib/clientplatform/state/data",
            "CLIENTPLATFORM_LOGS_DIR": "/var/log/clientplatform",
            "MPLCONFIGDIR": "/var/lib/clientplatform/state/matplotlib",
            "PREWARM_MARKER_PATH": "/var/lib/clientplatform/state/prewarm/audio.done",
            "TELEGRAM_TRANSPORT": "polling",
            "RUN_MODE": "polling",
            "TELEGRAM_WEBHOOK_ENABLED": "0",
            "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED": "0",
            "ALLOW_INSECURE_TELEGRAM_WEBHOOK": "0",
            "TELEGRAM_WEBHOOK_PREFIX": "",
            "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL": "",
            "TELEGRAM_WEBHOOK_SECRET_TOKEN": "",
            "MESSENGER_WEBHOOK_ENABLED": "1",
            "MESSENGER_WEBHOOK_HOST": "127.0.0.1",
            "MESSENGER_WEBHOOK_PORT": "8181",
            "MESSENGER_PUBLIC_BASE_URL": f"https://{domain}",
            "PAYMENT_PUBLIC_BASE_URL": f"https://{domain}",
            "PRIVACY_EXPORT_PUBLIC_BASE_URL": f"https://{domain}",
            "HEALTHCHECK_ENABLED": "1",
            "HEALTHCHECK_HOST": "127.0.0.1",
            "HEALTHCHECK_PORT": "8182",
            "HEALTHCHECK_DIAGNOSTICS_TOKEN": "d" * 48,
            "CLIENTPLATFORM_MEDIA_GATEWAY_ENABLED": "1",
            "CLIENTPLATFORM_MEDIA_GATEWAY_HOST": "127.0.0.1",
            "CLIENTPLATFORM_MEDIA_GATEWAY_PORT": "8191",
            "CLIENTPLATFORM_MEDIA_GATEWAY_BASE_URL": (
                f"https://{domain}/clientplatform"
            ),
            "CLIENTPLATFORM_MEDIA_GATEWAY_STORAGE_MODE": "s3",
            "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT": (
                "https://s3.production.internal"
            ),
            "CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION": "region-1",
            "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ACCESS_KEY_REFERENCE": (
                "secret://env/CLIENTPLATFORM_SECRET_S3_ACCESS_KEY"
            ),
            "CLIENTPLATFORM_MEDIA_GATEWAY_S3_SECRET_KEY_REFERENCE": (
                "secret://env/CLIENTPLATFORM_SECRET_S3_SECRET_KEY"
            ),
            "CLIENTPLATFORM_MEDIA_SIGNING_SECRET_REFERENCE": (
                "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
            ),
            "CLIENTPLATFORM_STORAGE_BUCKET": "clientplatform-production",
            "CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS": (
                "clientplatform-production"
            ),
            "CLIENTPLATFORM_SECRET_S3_ACCESS_KEY": "access-key-material",
            "CLIENTPLATFORM_SECRET_S3_SECRET_KEY": "secret-key-material",
            "CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY": "m" * 48,
            "CLIENTPLATFORM_S3_VERSIONING_ENABLED": "1",
            "CLIENTPLATFORM_S3_BACKUP_REPLICATION_ENABLED": "1",
            "CLIENTPLATFORM_BACKUP_DIR": "/var/backups/clientplatform/postgres",
            "CLIENTPLATFORM_RESTORE_EVIDENCE_DIR": (
                "/var/lib/clientplatform/restore-evidence"
            ),
            "CLIENTPLATFORM_BACKUP_RETENTION_DAYS": "30",
            "CLIENTPLATFORM_BACKUP_ENCRYPTION_REQUIRED": "1",
            "CLIENTPLATFORM_RESTORE_DRILL_REQUIRED": "1",
        }

    def test_valid_dedicated_systemd_environment_passes(self) -> None:
        self.assertEqual(validate_environment(self._valid_env()), [])

    def test_clientplatform_namespace_wins_over_conflicting_legacy_values(self) -> None:
        env = self._valid_env()
        env.update(
            {
                "METRO_DB_ENGINE": "sqlite",
                "METRO_DATA_DIR": "/app/legacy-data",
                "METRO_LOGS_DIR": "/tmp/legacy-logs",
            }
        )
        self.assertEqual(validate_environment(env), [])

    def test_legacy_database_and_path_names_remain_fallbacks(self) -> None:
        env = self._valid_env()
        env["METRO_DB_ENGINE"] = env.pop("CLIENTPLATFORM_DB_ENGINE")
        env["METRO_DATA_DIR"] = env.pop("CLIENTPLATFORM_DATA_DIR")
        env["METRO_LOGS_DIR"] = env.pop("CLIENTPLATFORM_LOGS_DIR")
        self.assertEqual(validate_environment(env), [])

    def test_shared_or_insecure_boundaries_fail_closed(self) -> None:
        cases = {
            "telegram_webhook": {
                "TELEGRAM_TRANSPORT": "webhook",
                "TELEGRAM_WEBHOOK_ENABLED": "1",
                "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL": (
                    "https://clientplatform.production.internal"
                ),
                "TELEGRAM_WEBHOOK_SECRET_TOKEN": "w" * 48,
                "TELEGRAM_WEBHOOK_PREFIX": "/telegram-webhook",
            },
            "shared_database": {
                "CLIENTPLATFORM_DATABASE_NAME": "metrotherapy",
                "DATABASE_URL": (
                    "postgresql://clientplatform_app:password@127.0.0.1:5432/"
                    "metrotherapy"
                ),
            },
            "shared_bucket": {
                "CLIENTPLATFORM_STORAGE_BUCKET": "metrotherapy-media",
                "CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS": "metrotherapy-media",
            },
            "shared_runtime": {
                "METRO_WRITABLE_ROOT": "/var/lib/metrotherapy/state"
            },
            "project_data": {"CLIENTPLATFORM_DATA_DIR": "/app/data"},
            "unexpected_webhook_secret": {
                "TELEGRAM_WEBHOOK_SECRET_TOKEN": "short"
            },
            "weak_diagnostics_secret": {
                "HEALTHCHECK_DIAGNOSTICS_TOKEN": "short"
            },
            "staging_secret": {
                "CLIENTPLATFORM_STAGING_TELEGRAM_BOT_TOKEN": "present"
            },
            "colliding_ports": {"HEALTHCHECK_PORT": "8181"},
            "wildcard_systemd": {"MESSENGER_WEBHOOK_HOST": "0.0.0.0"},
            "wrong_media_reference": {
                "CLIENTPLATFORM_MEDIA_SIGNING_SECRET_REFERENCE": (
                    "secret://env/SHARED_KEY"
                )
            },
        }
        for name, changes in cases.items():
            with self.subTest(name=name):
                env = self._valid_env()
                env.update(changes)
                self.assertTrue(validate_environment(env))

    def test_vk_and_max_webhooks_are_independent_from_telegram_polling(self) -> None:
        env = self._valid_env()
        env.update(
            {
                "VK_WEBHOOK_ENABLED": "1",
                "MAX_WEBHOOK_ENABLED": "1",
            }
        )
        self.assertEqual(validate_environment(env), [])

    def test_explicit_container_network_allows_internal_wildcard_binds(self) -> None:
        env = self._valid_env()
        env.update(
            {
                "CLIENTPLATFORM_DEPLOYMENT_MODE": "container",
                "CLIENTPLATFORM_CONTAINER_NETWORK_ISOLATED": "1",
                "MESSENGER_WEBHOOK_HOST": "0.0.0.0",
                "HEALTHCHECK_HOST": "0.0.0.0",
                "CLIENTPLATFORM_MEDIA_GATEWAY_HOST": "0.0.0.0",
            }
        )
        self.assertEqual(validate_environment(env), [])

    def test_production_templates_are_isolated_and_operational(self) -> None:
        root = Path(__file__).resolve().parents[1]
        env_example = (
            root / "deploy/clientplatform/clientplatform.production.env.example"
        ).read_text(encoding="utf-8")
        service = (root / "deploy/clientplatform/clientplatform.service").read_text(
            encoding="utf-8"
        )
        compose = (
            root / "deploy/clientplatform/compose.production.yml"
        ).read_text(encoding="utf-8")
        image_entrypoint = (
            root / "deploy/clientplatform/container-entrypoint.sh"
        ).read_text(encoding="utf-8")
        caddy = (root / "deploy/clientplatform/Caddyfile").read_text(
            encoding="utf-8"
        )
        runbook = (
            root / "docs/runbooks/CLIENTPLATFORM_PRODUCTION_ISOLATION.md"
        ).read_text(encoding="utf-8")
        dockerignore_path = root / "deploy/clientplatform/Dockerfile.dockerignore"
        root_dockerignore_path = root / ".dockerignore"
        dockerignore = dockerignore_path.read_text(encoding="utf-8")
        root_dockerignore = root_dockerignore_path.read_text(encoding="utf-8")
        self.assertIn(
            "CLIENTPLATFORM_DEPLOYMENT_ID=clientplatform-production", env_example
        )
        self.assertIn(
            "METRO_WRITABLE_ROOT=/var/lib/clientplatform/state", env_example
        )
        self.assertIn(
            "CLIENTPLATFORM_DATA_DIR=/var/lib/clientplatform/state/data", env_example
        )
        self.assertIn(
            "CLIENTPLATFORM_LOGS_DIR=/var/log/clientplatform", env_example
        )
        self.assertIn("CLIENTPLATFORM_DB_ENGINE=postgres", env_example)
        self.assertIn("TELEGRAM_TRANSPORT=polling", env_example)
        self.assertIn("TELEGRAM_WEBHOOK_ENABLED=0", env_example)
        self.assertIn("MESSENGER_WEBHOOK_ENABLED=1", env_example)
        self.assertIn("CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED=0", env_example)
        self.assertIn("/clientplatform/webhooks/vk/*", caddy)
        self.assertIn("/clientplatform/webhooks/max/*", caddy)
        self.assertIn("CLIENTPLATFORM_BOT_GATEWAY_ENABLED=1", env_example)
        self.assertIn("CLIENTPLATFORM_BOT_GATEWAY_POLL_TIMEOUT_SEC=20", env_example)
        self.assertNotIn(
            "DATABASE_URL=postgresql://localhost:5432/metrotherapy", env_example
        )
        self.assertIn("clientplatform_production_preflight.py", service)
        self.assertIn("clientplatform_bot_gateway_preflight.py", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("clientplatform-postgres", compose)
        self.assertIn("CLIENTPLATFORM_CONTAINER_NETWORK_ISOLATED", compose)
        self.assertNotIn('entrypoint: ["/bin/sh", "-ec"]', compose)
        self.assertNotIn("clientplatform_bot_gateway_preflight.py", compose)
        self.assertIn(
            "python -m scripts.clientplatform_production_preflight",
            image_entrypoint,
        )
        self.assertIn(
            "python -m scripts.clientplatform_monetization_preflight",
            image_entrypoint,
        )
        self.assertIn(
            "python -m scripts.clientplatform_program_media_preflight",
            image_entrypoint,
        )
        self.assertIn(
            "python -m scripts.clientplatform_bot_gateway_preflight",
            image_entrypoint,
        )
        self.assertIn("exec python main.py", image_entrypoint)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("/webhooks/*", caddy)
        self.assertNotIn("/telegram-webhook", caddy)
        self.assertNotIn("/clientplatform/managed-bots/*", caddy)
        self.assertIn("/clientplatform/*", caddy)
        self.assertIn("restore-drill", runbook)
        self.assertIn("CLIENTPLATFORM_RESTORE_ADMIN_DATABASE_URL", runbook)
        self.assertIn(
            "Managed Client Bots share the same application process", runbook
        )
        self.assertIn(
            "POSTGRES_BOT_GATEWAY_CONCURRENCY_OK",
            (
                root / "scripts/probe_postgres_bot_gateway_concurrency.py"
            ).read_text(encoding="utf-8"),
        )
        self.assertTrue(dockerignore_path.is_file())
        self.assertTrue(root_dockerignore_path.is_file())
        for ignore in (dockerignore, root_dockerignore):
            self.assertIn("deploy/clientplatform/clientplatform.env", ignore)
            self.assertIn("**/.env", ignore)
            self.assertIn("**/.env.*", ignore)
            self.assertIn("**/*.env", ignore)
            self.assertIn("!**/*.env.example", ignore)
            self.assertIn("deploy/clientplatform/Dockerfile.server", ignore)
            self.assertIn("deploy/clientplatform/*.before-*", ignore)
            self.assertIn("deploy/clientplatform/*.failed", ignore)

    def test_backup_helpers_reject_non_clientplatform_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-ClientPlatform"):
            _database_name("postgresql://user:password@db:5432/metrotherapy")
        env = _pg_environment(
            "postgresql://clientplatform_app:very-secret@db:5432/clientplatform"
        )
        self.assertEqual(env["PGDATABASE"], "clientplatform")
        self.assertEqual(env["PGPASSWORD"], "very-secret")
        admin = _pg_environment(
            "postgresql://restore_admin:admin-secret@db:5432/postgres",
            clientplatform_only=False,
        )
        self.assertEqual(admin["PGDATABASE"], "postgres")
        self.assertEqual(admin["PGUSER"], "restore_admin")
        self.assertEqual(
            _safe_identifier("clientplatform_restore_1"),
            "clientplatform_restore_1",
        )
        with self.assertRaisesRegex(ValueError, "unsafe"):
            _safe_identifier("clientplatform;drop")

    def test_probe_helpers_are_bounded_and_parse_sanitized_jsonl(self) -> None:
        self.assertEqual(_join("https://host/", "/readyz"), "https://host/readyz")
        self.assertEqual(_percentile([0.1, 0.2, 0.3, 0.4], 0.95), 0.3)
        with tempfile.TemporaryDirectory() as temp:
            fixture = Path(temp) / "events.jsonl"
            fixture.write_text(
                '{"update_id":1}\n{"update_id":2}\n', encoding="utf-8"
            )
            self.assertEqual(len(_load_replay_events(fixture)), 2)

    def test_workflow_has_postgres_restore_drill(self) -> None:
        workflow = Path(
            ".github/workflows/clientplatform-production-isolation.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("postgres:16", workflow)
        self.assertIn("clientplatform_production_preflight.py", workflow)
        self.assertIn("clientplatform_postgres_backup.py backup", workflow)
        self.assertIn("clientplatform_postgres_backup.py restore-drill", workflow)
        self.assertIn("CLIENTPLATFORM_RESTORE_ADMIN_DATABASE_URL", workflow)
        self.assertIn("docker build", workflow)
        self.assertIn("docker compose", workflow)
        self.assertNotIn("@v4", workflow)
        self.assertNotIn("@v5", workflow)


if __name__ == "__main__":
    unittest.main()
