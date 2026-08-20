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
from scripts import clientplatform_container_audio_assets as container_audio
from scripts import clientplatform_prepare_production_env as prepare_env
from scripts import clientplatform_production_deploy as production_deploy
from services.audio_asset_integrity import validate_release_assets


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
            self.assertIn("CLIENTPLATFORM_REQUIRE_AUDIO_ASSETS", added)
            self.assertNotIn("CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY", added)
            self.assertIn("CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY=existing-key", first)
            self.assertIn("CLIENTPLATFORM_REQUIRE_AUDIO_ASSETS=1", first)
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

            path.write_text(
                _REQUIRED_ENV + "CLIENTPLATFORM_REQUIRE_AUDIO_ASSETS=0\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(
                prepare_env.EnvironmentPreparationError,
                "mismatched_clientplatform_require_audio_assets",
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


class ContainerAudioAssetBuildTests(unittest.TestCase):
    @staticmethod
    def _write_audio_tree(root: Path) -> None:
        audio = root / "audio"
        (audio / "demo").mkdir(parents=True)
        (audio / "full").mkdir(parents=True)
        (audio / "demo" / "work.ogg").write_bytes(b"OggS" + b"w" * 128)
        (audio / "demo" / "home.ogg").write_bytes(b"OggS" + b"h" * 128)
        (audio / "full" / "1_work.ogg").write_bytes(b"OggS" + b"1" * 128)
        (audio / "full" / "2_home.ogg").write_bytes(b"OggS" + b"2" * 128)

    def test_prepare_seals_moves_links_and_verifies_audio(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "app"
            assets = Path(raw) / "immutable-audio"
            root.mkdir()
            self._write_audio_tree(root)

            first = container_audio.prepare_container_audio_assets(
                root=root,
                asset_root=assets,
                require=True,
            )
            second = container_audio.prepare_container_audio_assets(
                root=root,
                asset_root=assets,
                require=True,
            )

            self.assertIsNotNone(first)
            self.assertEqual(first, second)
            assert first is not None
            self.assertTrue((root / "audio").is_symlink())
            self.assertEqual((root / "audio").resolve(), assets / first.asset_sha256)
            self.assertTrue((root / ".audio-assets.json").is_file())
            self.assertEqual(
                validate_release_assets(root, require_versioned=True),
                first,
            )

    def test_prepare_fails_closed_when_required_audio_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "app"
            root.mkdir()
            with self.assertRaisesRegex(
                container_audio.ContainerAudioAssetError,
                "container_audio_source_missing",
            ):
                container_audio.prepare_container_audio_assets(
                    root=root,
                    asset_root=Path(raw) / "immutable-audio",
                    require=True,
                )


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

    def test_baseline_gate_uses_persistent_readiness_not_aged_startup_logs(self) -> None:
        with (
            mock.patch.object(production_deploy, "_container_running", return_value=True),
            mock.patch.object(production_deploy, "_ready", return_value=True),
            mock.patch.object(production_deploy, "_runtime_markers") as markers,
        ):
            production_deploy._wait_for_baseline_readiness(60)
        markers.assert_not_called()

    def test_rollback_retags_recreates_and_rechecks_availability(self) -> None:
        compose = ["docker", "compose", "--env-file", ".env"]
        with (
            mock.patch.object(production_deploy, "_run") as run,
            mock.patch.object(production_deploy, "_wait_for_baseline_readiness") as wait,
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
                "_wait_for_baseline_readiness",
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
                "_wait_for_baseline_readiness",
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

    def test_explicit_recovery_updates_an_unavailable_baseline_without_false_rollback(self) -> None:
        compose = ["docker", "compose", "--env-file", "clientplatform.env"]
        previous_image = "sha256:" + "b" * 64
        evidence_path = Path("/evidence/recovery.json")
        evidence_payload: dict[str, object] = {}

        def write_evidence(payload: dict[str, object]) -> Path:
            evidence_payload.update(payload)
            return evidence_path

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
                "_wait_for_baseline_readiness",
                side_effect=production_deploy.DeploymentError("forward-schema-old-image"),
            ),
            mock.patch.object(production_deploy, "_local_backup", return_value=Path("/backup.dump")),
            mock.patch.object(production_deploy, "_container_image", return_value=previous_image),
            mock.patch.object(production_deploy, "_wait_for_visual_gateway") as wait_gateway,
            mock.patch.object(production_deploy, "_wait_for_readiness") as wait,
            mock.patch.object(production_deploy, "_external_https") as external,
            mock.patch.object(
                production_deploy,
                "_sales_operations_smoke",
                return_value={
                    "contract_version": "u008-u009-sales-operations-v2",
                    "ok": True,
                    "rollback_clean": True,
                    "checks": {name: True for name in production_deploy._SALES_SMOKE_REQUIRED_CHECKS},
                    "residue": {"businesses": 0},
                },
            ) as sales_smoke,
            mock.patch.object(production_deploy, "_write_evidence", side_effect=write_evidence),
            mock.patch.object(production_deploy, "_rollback") as rollback,
        ):
            result = production_deploy.deploy(
                allow_local_backup=True,
                timeout_seconds=240,
                recover_unavailable_baseline=True,
            )

        self.assertEqual(result, evidence_path)
        wait_gateway.assert_called_once_with(240)
        wait.assert_called_once_with(240)
        external.assert_called_once_with("clientplatform.example.test")
        sales_smoke.assert_called_once_with()
        rollback.assert_not_called()
        self.assertTrue(evidence_payload["recovery_mode"])
        self.assertEqual(
            evidence_payload["sales_operations_smoke"]["contract_version"],
            "u008-u009-sales-operations-v2",
        )
        self.assertFalse(evidence_payload["baseline_ready"])

        image_tag_calls = [
            call
            for call in run.call_args_list
            if call.args
            and call.args[0][:3] == ["docker", "image", "tag"]
        ]
        self.assertEqual(len(image_tag_calls), 2)
        self.assertEqual(image_tag_calls[0].args[0][3], previous_image)
        self.assertEqual(image_tag_calls[1].args[0][3], previous_image)
        self.assertEqual(
            image_tag_calls[1].args[0][4],
            "clientplatform-production-visual-gateway:release-" + "a" * 40,
        )

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
        rollback_tag_index = source.index(
            '_run(["docker", "image", "tag", previous_image, rollback_tag])'
        )
        gateway_build_index = source.index('_run([*compose, "build", "visual-gateway"])')
        build_index = source.index('_run([*compose, "build", "app", "backup"])')
        gateway_recreate_index = source.index(
            '"--force-recreate", "visual-gateway"',
            gateway_build_index,
        )
        recreate_index = source.index(
            '"--force-recreate", "app", "caddy"',
            build_index,
        )

        self.assertLess(baseline_index, backup_index)
        self.assertLess(backup_index, rollback_tag_index)
        self.assertLess(rollback_tag_index, gateway_build_index)
        self.assertLess(gateway_build_index, build_index)
        self.assertLess(build_index, gateway_recreate_index)
        self.assertLess(gateway_recreate_index, recreate_index)
        self.assertIn("production_baseline_readiness_timeout", source)
        self.assertIn("production_readiness_timeout", source)
        self.assertIn("production_startup_timeout", source)
        self.assertIn("external_https_proof_failed", source)
        self.assertIn("sales_operations_smoke = _sales_operations_smoke()", source)
        self.assertIn('"sales_operations_smoke": sales_operations_smoke', source)
        self.assertIn("rollback_tag", source)
        self.assertIn('"--no-build", "--force-recreate"', source)
        self.assertIn("CLIENTPLATFORM_PRODUCTION_ROLLBACK_OK", source)
        self.assertIn("CLIENTPLATFORM_PRODUCTION_RECOVERY_FAILED", source)
        self.assertIn("CLIENTPLATFORM_PRODUCTION_DEPLOY_FAILED", source)
        self.assertIn("deployment_failed_and_rollback_failed", source)
        self.assertIn("CLIENTPLATFORM_PRODUCTION_DEPLOY_OK", source)
        self.assertIn("--recover-unavailable-baseline", source)
        self.assertNotIn("shell=True", source)


    def test_sales_operations_smoke_requires_complete_rollback_clean_contract(self) -> None:
        payload = {
            "contract_version": "u008-u009-sales-operations-v2",
            "ok": True,
            "rollback_clean": True,
            "checks": {name: True for name in production_deploy._SALES_SMOKE_REQUIRED_CHECKS},
            "residue": {"businesses": 0, "clientplatform_sales_leads": 0},
        }
        completed = __import__("subprocess").CompletedProcess(
            args=["docker", "exec"],
            returncode=0,
            stdout=(
                "noise before marker\n"
                "CLIENTPLATFORM_SALES_PRODUCTION_SMOKE_OK:"
                + __import__("json").dumps(payload)
                + "\n"
            ),
            stderr="",
        )
        with mock.patch.object(production_deploy, "_run", return_value=completed):
            self.assertEqual(production_deploy._sales_operations_smoke(), payload)

    def test_sales_operations_smoke_rejects_rollback_residue(self) -> None:
        payload = {
            "contract_version": "u008-u009-sales-operations-v2",
            "ok": True,
            "rollback_clean": True,
            "checks": {name: True for name in production_deploy._SALES_SMOKE_REQUIRED_CHECKS},
            "residue": {"businesses": 1},
        }
        completed = __import__("subprocess").CompletedProcess(
            args=["docker", "exec"],
            returncode=0,
            stdout="CLIENTPLATFORM_SALES_PRODUCTION_SMOKE_OK:" + __import__("json").dumps(payload),
            stderr="",
        )
        with mock.patch.object(production_deploy, "_run", return_value=completed):
            with self.assertRaisesRegex(
                production_deploy.DeploymentError,
                "sales_production_smoke_rollback_not_clean",
            ):
                production_deploy._sales_operations_smoke()

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

    def test_production_image_uses_official_postgres_and_seals_audio(self) -> None:
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
        self.assertIn("ARG CLIENTPLATFORM_REQUIRE_AUDIO_ASSETS=0", dockerfile)
        self.assertIn("python -m scripts.clientplatform_container_audio_assets", dockerfile)
        self.assertIn("clientplatform_sales_production_smoke.py", dockerfile)
        self.assertIn("test -L /app/audio", dockerfile)
        self.assertIn("test -r /app/.audio-assets.json", dockerfile)
        self.assertNotIn("apt.postgresql.org", dockerfile)
        self.assertNotIn("www.postgresql.org", dockerfile)
        self.assertNotIn("ACCC4CF8", dockerfile)
        self.assertNotIn("postgresql-client-16", dockerfile)

    def test_compose_requires_audio_for_app_only(self) -> None:
        compose = (
            Path(production_deploy.ROOT) / "deploy/clientplatform/compose.production.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "CLIENTPLATFORM_REQUIRE_AUDIO_ASSETS: ${CLIENTPLATFORM_REQUIRE_AUDIO_ASSETS:-0}",
            compose,
        )
        self.assertGreaterEqual(
            compose.count('CLIENTPLATFORM_REQUIRE_AUDIO_ASSETS: "0"'),
            2,
        )

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
