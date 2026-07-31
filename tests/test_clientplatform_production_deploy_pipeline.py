from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from config import settings as runtime_settings
from scripts import clientplatform_prepare_production_env as prepare_env
from scripts import clientplatform_production_deploy as production_deploy


_REQUIRED_ENV = """\
CLIENTPLATFORM_DOMAIN=clientplatform.example.test
CLIENTPLATFORM_STORAGE_BUCKET=clientplatform-production-8493913
CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT=https://s3.example.test
CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION=ru-1
CLIENTPLATFORM_SECRET_S3_ACCESS_KEY=access-secret
CLIENTPLATFORM_SECRET_S3_SECRET_KEY=secret-secret
"""


class ProductionEnvironmentPreparationTests(unittest.TestCase):
    def test_prepare_is_idempotent_and_never_replaces_existing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clientplatform.env"
            path.write_text(
                _REQUIRED_ENV + "CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY=existing-key\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)

            added = prepare_env.prepare(path)
            first = path.read_text(encoding="utf-8")
            second_added = prepare_env.prepare(path)
            second = path.read_text(encoding="utf-8")

            self.assertIn("CLIENTPLATFORM_PROGRAM_MEDIA_INGEST_ENABLED", added)
            self.assertNotIn("CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY", added)
            self.assertIn("CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY=existing-key", first)
            self.assertEqual(second_added, ())
            self.assertEqual(first, second)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                path.with_name(path.name + ".before-current-main").stat().st_mode & 0o777,
                0o600,
            )

    def test_prepare_generates_signing_key_without_using_business_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clientplatform.env"
            path.write_text(_REQUIRED_ENV, encoding="utf-8")
            os.chmod(path, 0o600)

            added = prepare_env.prepare(path)
            payload = path.read_text(encoding="utf-8")

            self.assertIn("CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY", added)
            signing_lines = [
                line
                for line in payload.splitlines()
                if line.startswith("CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY=")
            ]
            self.assertEqual(len(signing_lines), 1)
            self.assertGreater(len(signing_lines[0].split("=", 1)[1]), 40)
            self.assertNotIn("8493913", signing_lines[0])

    def test_prepare_rejects_world_readable_symlinked_or_mismatched_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            path = directory / "clientplatform.env"
            path.write_text(_REQUIRED_ENV, encoding="utf-8")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(
                prepare_env.EnvironmentPreparationError,
                "permissions_must_be_0600",
            ):
                prepare_env.prepare(path)

            os.chmod(path, 0o600)
            link = directory / "linked.env"
            link.symlink_to(path)
            with self.assertRaisesRegex(
                prepare_env.EnvironmentPreparationError,
                "regular_file",
            ):
                prepare_env.prepare(link)

            path.write_text(
                _REQUIRED_ENV + "CLIENTPLATFORM_PUBLIC_BASE_URL=https://wrong.test\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(
                prepare_env.EnvironmentPreparationError,
                "mismatched_clientplatform_public_base_url",
            ):
                prepare_env.prepare(path)

    def test_trusted_proxy_parser_ignores_empty_csv_segments(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "TRUST_PROXY_HEADERS": "1",
                "PAYMENT_WEBHOOK_TRUSTED_PROXY_CIDRS": ",172.18.0.0/16,,",
            },
            clear=False,
        ):
            runtime_settings._validate_trusted_proxy_env()


class ProductionDeploymentContractTests(unittest.TestCase):
    def test_backup_checksum_is_streamed(self) -> None:
        payload = b"clientplatform" * 1024
        self.assertEqual(
            production_deploy._sha256_stream(io.BytesIO(payload)),
            __import__("hashlib").sha256(payload).hexdigest(),
        )

    def test_startup_gate_checks_availability_without_requiring_full_readiness(self) -> None:
        with (
            mock.patch.object(production_deploy, "_container_running", return_value=True),
            mock.patch.object(production_deploy, "_healthy", return_value=True),
            mock.patch.object(production_deploy, "_runtime_markers", return_value=True),
            mock.patch.object(production_deploy, "_ready", return_value=False) as ready,
        ):
            production_deploy._wait_for_startup(60)
        ready.assert_not_called()

    def test_rollback_retags_recreates_and_rechecks_availability(self) -> None:
        compose = ["docker", "compose", "--env-file", ".env"]
        with (
            mock.patch.object(production_deploy, "_run") as run,
            mock.patch.object(production_deploy, "_wait_for_startup") as wait,
            mock.patch.object(production_deploy, "_external_https") as external,
        ):
            production_deploy._rollback(
                compose=compose,
                rollback_tag="clientplatform-production-app:rollback-proof",
                domain="clientplatform.example.test",
                timeout_seconds=120,
            )

        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    [
                        "docker",
                        "image",
                        "tag",
                        "clientplatform-production-app:rollback-proof",
                        "clientplatform-production-app:latest",
                    ]
                ),
                mock.call(
                    [
                        *compose,
                        "up",
                        "-d",
                        "--no-build",
                        "--force-recreate",
                        "app",
                        "caddy",
                    ]
                ),
            ],
        )
        wait.assert_called_once_with(120)
        external.assert_called_once_with("clientplatform.example.test")

    def test_rollback_fails_closed_when_recovered_image_is_unavailable(self) -> None:
        with (
            mock.patch.object(production_deploy, "_run"),
            mock.patch.object(
                production_deploy,
                "_wait_for_startup",
                side_effect=production_deploy.DeploymentError("not-started"),
            ),
            mock.patch.object(production_deploy, "_external_https") as external,
        ):
            with self.assertRaisesRegex(
                production_deploy.DeploymentError,
                "rollback_not_available",
            ):
                production_deploy._rollback(
                    compose=["docker", "compose"],
                    rollback_tag="clientplatform-production-app:rollback-proof",
                    domain="clientplatform.example.test",
                    timeout_seconds=60,
                )
        external.assert_not_called()

    def test_existing_unready_production_aborts_before_any_container_change(self) -> None:
        compose = ["docker", "compose", "--env-file", "clientplatform.env"]
        with (
            mock.patch.object(production_deploy.os, "geteuid", return_value=0),
            mock.patch.object(production_deploy, "prepare"),
            mock.patch.object(
                production_deploy,
                "_env_values",
                return_value={"CLIENTPLATFORM_DOMAIN": "clientplatform.example.test"},
            ),
            mock.patch.object(production_deploy, "_git_sha", return_value="a" * 40),
            mock.patch.object(production_deploy, "_compose", return_value=compose),
            mock.patch.object(production_deploy, "_run") as run,
            mock.patch.object(production_deploy, "_container_exists", return_value=True),
            mock.patch.object(
                production_deploy,
                "_wait_for_readiness",
                side_effect=production_deploy.DeploymentError("red-baseline"),
            ),
            mock.patch.object(production_deploy, "_external_https") as external,
        ):
            with self.assertRaisesRegex(
                production_deploy.DeploymentError,
                "production_not_ready_before_deploy",
            ):
                production_deploy.deploy(allow_local_backup=True, timeout_seconds=240)

        self.assertEqual(run.call_args_list, [mock.call([*compose, "config", "--quiet"])])
        external.assert_not_called()

    def test_main_reports_expected_failure_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    production_deploy,
                    "LOCK_PATH",
                    Path(raw) / "clientplatform-production-deploy.lock",
                ),
                mock.patch.object(
                    production_deploy,
                    "deploy",
                    side_effect=production_deploy.DeploymentError("operator-safe-proof"),
                ),
                mock.patch.object(sys, "argv", ["clientplatform_production_deploy"]),
                redirect_stderr(stderr),
            ):
                result = production_deploy.main()

        self.assertEqual(result, 1)
        self.assertEqual(
            stderr.getvalue().strip(),
            "CLIENTPLATFORM_PRODUCTION_DEPLOY_FAILED:operator-safe-proof",
        )
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_deploy_contract_orders_baseline_backup_build_and_recreate(self) -> None:
        source = Path(production_deploy.__file__).read_text(encoding="utf-8")
        baseline_index = source.index("production_not_ready_before_deploy")
        backup_index = source.index("backup_reference =")
        build_index = source.index('_run([*compose, "build", "app", "backup"])')
        recreate_index = source.index(
            '"--force-recreate", "app", "caddy"',
            build_index,
        )

        self.assertLess(baseline_index, backup_index)
        self.assertLess(backup_index, build_index)
        self.assertLess(build_index, recreate_index)
        self.assertIn("production_readiness_timeout", source)
        self.assertIn("production_startup_timeout", source)
        self.assertIn("external_https_proof_failed", source)
        self.assertIn("rollback_tag", source)
        self.assertIn('"--no-build", "--force-recreate"', source)
        self.assertIn("CLIENTPLATFORM_PRODUCTION_ROLLBACK_OK", source)
        self.assertIn("CLIENTPLATFORM_PRODUCTION_DEPLOY_FAILED", source)
        self.assertIn("deployment_failed_and_rollback_failed", source)
        self.assertIn("CLIENTPLATFORM_PRODUCTION_DEPLOY_OK", source)
        self.assertNotIn("shell=True", source)

    def test_source_updater_preserves_env_and_requires_expected_sha_when_set(self) -> None:
        updater = (
            Path(production_deploy.ROOT)
            / "deploy"
            / "clientplatform"
            / "update-production.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('git fetch --no-tags --prune --depth 1 origin "$TARGET_REF"', updater)
        self.assertIn('TARGET_SHA=$(git rev-parse FETCH_HEAD)', updater)
        self.assertIn('EXPECTED_SHA=${CLIENTPLATFORM_EXPECTED_SHA:-}', updater)
        self.assertIn('"$TARGET_SHA" != "$EXPECTED_SHA"', updater)
        self.assertIn('git reset --hard "$TARGET_SHA"', updater)
        self.assertNotIn("git clean", updater)
        self.assertIn("clientplatform.env", updater)
        self.assertIn(
            'exec python3 -m scripts.clientplatform_production_deploy "$@"',
            updater,
        )
        self.assertNotIn("python3 scripts/clientplatform_production_deploy.py", updater)

    def test_production_image_uses_official_postgres_toolchain_without_pgdg_fetch(self) -> None:
        dockerfile = (
            Path(production_deploy.ROOT) / "deploy/clientplatform/Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("FROM python:3.12-slim-bookworm AS python-runtime", dockerfile)
        self.assertIn("FROM postgres:16-bookworm", dockerfile)
        self.assertIn("COPY --from=python-runtime /usr/local /usr/local", dockerfile)
        self.assertIn("pg_dump --version", dockerfile)
        self.assertIn("pg_restore --version", dockerfile)
        self.assertIn("psql --version", dockerfile)
        self.assertIn("Acquire::Retries=5", dockerfile)
        self.assertNotIn("apt.postgresql.org", dockerfile)
        self.assertNotIn("www.postgresql.org", dockerfile)
        self.assertNotIn("ACCC4CF8", dockerfile)
        self.assertNotIn("postgresql-client-16", dockerfile)

    def test_app_image_owns_preflights_and_compose_does_not_override_startup(self) -> None:
        root = Path(production_deploy.ROOT)
        dockerfile = (root / "deploy/clientplatform/Dockerfile").read_text(
            encoding="utf-8"
        )
        compose = (root / "deploy/clientplatform/compose.production.yml").read_text(
            encoding="utf-8"
        )
        entrypoint = (
            root / "deploy/clientplatform/container-entrypoint.sh"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'ENTRYPOINT ["/app/deploy/clientplatform/container-entrypoint.sh"]',
            dockerfile,
        )
        for module in (
            "clientplatform_production_preflight",
            "clientplatform_monetization_preflight",
            "clientplatform_program_media_preflight",
            "clientplatform_bot_gateway_preflight",
        ):
            self.assertIn(f"python -m scripts.{module}", entrypoint)
            self.assertIn(f"/app/scripts/{module}.py", dockerfile)
        self.assertIn("exec python main.py", entrypoint)
        self.assertNotIn('entrypoint: ["/bin/sh", "-ec"]', compose)
        self.assertNotIn("python scripts/clientplatform_production_preflight.py", compose)
        self.assertNotIn("python scripts/clientplatform_monetization_preflight.py", compose)
        self.assertNotIn("python scripts/clientplatform_program_media_preflight.py", compose)
        self.assertNotIn("python scripts/clientplatform_bot_gateway_preflight.py", compose)


if __name__ == "__main__":
    unittest.main()
