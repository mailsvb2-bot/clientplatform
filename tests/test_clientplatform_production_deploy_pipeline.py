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

    def test_disk_guard_fails_closed_on_critical_usage_or_low_free_space(self) -> None:
        critical = {
            "total_bytes": 30 * 1024**3,
            "used_bytes": 24 * 1024**3,
            "free_bytes": 6 * 1024**3,
            "used_percent": 75.0,
        }
        low_free = {
            "total_bytes": 100 * 1024**3,
            "used_bytes": 95 * 1024**3,
            "free_bytes": 5 * 1024**3,
            "used_percent": 95.0,
        }
        for capacity in (critical, low_free):
            with (
                self.subTest(capacity=capacity),
                mock.patch.object(production_deploy, "_disk_capacity", return_value=capacity),
            ):
                with self.assertRaisesRegex(
                    production_deploy.DeploymentError,
                    "insufficient_disk_capacity_before_deploy",
                ):
                    production_deploy._assert_deploy_disk_capacity()

    def test_disk_guard_allows_healthy_capacity(self) -> None:
        capacity = {
            "total_bytes": 30 * 1024**3,
            "used_bytes": 18 * 1024**3,
            "free_bytes": 12 * 1024**3,
            "used_percent": 60.0,
        }
        with mock.patch.object(production_deploy, "_disk_capacity", return_value=capacity):
            self.assertEqual(production_deploy._assert_deploy_disk_capacity(), capacity)

    def test_deploy_image_retention_keeps_running_and_recent_rollback_images(self) -> None:
        app_rollbacks = [
            f"{production_deploy.APP_IMAGE}:rollback-20260820T200000Z",
            f"{production_deploy.APP_IMAGE}:rollback-20260819T200000Z",
            f"{production_deploy.APP_IMAGE}:rollback-20260818T200000Z",
        ]
        recovered = f"{production_deploy.APP_IMAGE}:recovered-legacy"
        visual_rollbacks = [
            f"{production_deploy.VISUAL_GATEWAY_IMAGE}:rollback-20260820T200000Z",
            f"{production_deploy.VISUAL_GATEWAY_IMAGE}:rollback-20260819T200000Z",
        ]
        target_sha = "f" * 40
        target_release = f"{production_deploy.VISUAL_GATEWAY_IMAGE}:release-{target_sha}"
        current_release = f"{production_deploy.VISUAL_GATEWAY_IMAGE}:release-current"
        previous_release = f"{production_deploy.VISUAL_GATEWAY_IMAGE}:release-previous"
        ancient_release = f"{production_deploy.VISUAL_GATEWAY_IMAGE}:release-ancient"
        tags = {
            production_deploy.APP_IMAGE: [*app_rollbacks, recovered],
            production_deploy.VISUAL_GATEWAY_IMAGE: [
                *visual_rollbacks,
                target_release,
                current_release,
                previous_release,
                ancient_release,
            ],
        }
        image_ids = {
            app_rollbacks[0]: "sha256:app-recent",
            app_rollbacks[1]: "sha256:app-running",
            app_rollbacks[2]: "sha256:app-old",
            recovered: "sha256:app-recovered-old",
            visual_rollbacks[0]: "sha256:visual-previous",
            visual_rollbacks[1]: "sha256:visual-old",
            target_release: "sha256:target-retry",
            current_release: "sha256:visual-running",
            previous_release: "sha256:visual-previous",
            ancient_release: "sha256:visual-old",
        }

        with (
            mock.patch.object(
                production_deploy,
                "_running_image_ids",
                return_value={"sha256:app-running", "sha256:visual-running"},
            ),
            mock.patch.object(
                production_deploy,
                "_image_tags",
                side_effect=lambda repository: tags[repository],
            ),
            mock.patch.object(
                production_deploy,
                "_image_id",
                side_effect=lambda reference: image_ids[reference],
            ),
            mock.patch.object(production_deploy, "_run") as run,
        ):
            result = production_deploy._prune_deploy_image_history(target_sha)

        removed = [
            call.args[0][-1]
            for call in run.call_args_list
            if call.args and call.args[0][:3] == ["docker", "image", "rm"]
        ]
        self.assertEqual(
            removed,
            [app_rollbacks[2], recovered, visual_rollbacks[1], ancient_release],
        )
        self.assertEqual(result["removed_tags"], 4)
        self.assertEqual(result["app_rollbacks_retained_before_deploy"], 1)
        self.assertEqual(result["visual_rollbacks_retained_before_deploy"], 1)
        self.assertNotIn(mock.call(["docker", "image", "prune", "--force"]), run.call_args_list)

    def test_predeploy_current_baseline_supersedes_old_rollback_generation(self) -> None:
        new_app = f"{production_deploy.APP_IMAGE}:rollback-20260903T093900Z"
        old_app = f"{production_deploy.APP_IMAGE}:rollback-20260903T052203Z"
        new_visual = f"{production_deploy.VISUAL_GATEWAY_IMAGE}:rollback-20260903T093900Z"
        old_visual = f"{production_deploy.VISUAL_GATEWAY_IMAGE}:rollback-20260903T052203Z"
        tags = {
            production_deploy.APP_IMAGE: [new_app, old_app],
            production_deploy.VISUAL_GATEWAY_IMAGE: [new_visual, old_visual],
        }
        image_ids = {
            new_app: "sha256:app-current",
            old_app: "sha256:app-old",
            new_visual: "sha256:visual-current",
            old_visual: "sha256:visual-old",
        }
        with (
            mock.patch.object(
                production_deploy,
                "_running_image_ids",
                return_value={"sha256:app-current", "sha256:visual-current"},
            ),
            mock.patch.object(
                production_deploy,
                "_image_tags",
                side_effect=lambda repository: tags[repository],
            ),
            mock.patch.object(
                production_deploy,
                "_image_id",
                side_effect=lambda reference: image_ids[reference],
            ),
            mock.patch.object(production_deploy, "_run") as run,
        ):
            result = production_deploy._prune_deploy_image_history("f" * 40)

        removed = [
            call.args[0][-1]
            for call in run.call_args_list
            if call.args and call.args[0][:3] == ["docker", "image", "rm"]
        ]
        self.assertEqual(removed, [old_app, old_visual])
        self.assertEqual(result["app_rollbacks_retained_before_deploy"], 1)
        self.assertEqual(result["visual_rollbacks_retained_before_deploy"], 1)

    def test_build_cache_retention_is_bounded(self) -> None:
        with mock.patch.object(production_deploy, "_run") as run:
            result = production_deploy._prune_build_cache()

        run.assert_called_once_with(
            [
                "docker",
                "builder",
                "prune",
                "--force",
                "--all",
                "--keep-storage",
                "2GB",
            ]
        )
        self.assertEqual(result, {"keep_storage": "2GB"})

    def test_build_cache_capacity_cleanup_uses_bounded_mode_with_healthy_headroom(self) -> None:
        healthy = {
            "total_bytes": 30 * 1024**3,
            "used_bytes": 18 * 1024**3,
            "free_bytes": 12 * 1024**3,
            "used_percent": 60.0,
        }
        with (
            mock.patch.object(
                production_deploy,
                "_disk_capacity",
                side_effect=[healthy, healthy],
            ),
            mock.patch.object(
                production_deploy,
                "_prune_build_cache",
                return_value={"keep_storage": "2GB"},
            ) as prune,
        ):
            result = production_deploy._prune_build_cache_for_capacity()

        prune.assert_called_once_with()
        self.assertEqual(result["mode"], "bounded")
        self.assertEqual(result["keep_storage"], "2GB")
        self.assertFalse(result["pressure_cleanup_applied"])
        self.assertEqual(result["after_cleanup"], healthy)

    def test_build_cache_capacity_cleanup_uses_full_prune_under_pressure(self) -> None:
        critical = {
            "total_bytes": 30 * 1024**3,
            "used_bytes": 24 * 1024**3,
            "free_bytes": 6 * 1024**3,
            "used_percent": 80.0,
        }
        recovered = {
            "total_bytes": 30 * 1024**3,
            "used_bytes": 21 * 1024**3,
            "free_bytes": 9 * 1024**3,
            "used_percent": 70.0,
        }
        with (
            mock.patch.object(
                production_deploy,
                "_disk_capacity",
                side_effect=[critical, recovered],
            ),
            mock.patch.object(
                production_deploy,
                "_prune_build_cache",
                return_value={"keep_storage": "0B"},
            ) as prune,
        ):
            result = production_deploy._prune_build_cache_for_capacity()

        prune.assert_called_once_with(pressure=True)
        self.assertEqual(result["mode"], "pressure_full")
        self.assertEqual(result["keep_storage"], "0B")
        self.assertTrue(result["pressure_cleanup_applied"])
        self.assertEqual(result["after_cleanup"], recovered)

    def test_build_cache_pressure_failure_keeps_hard_disk_guard(self) -> None:
        critical = {
            "total_bytes": 30 * 1024**3,
            "used_bytes": 24 * 1024**3,
            "free_bytes": 6 * 1024**3,
            "used_percent": 80.0,
        }
        with (
            mock.patch.object(
                production_deploy,
                "_disk_capacity",
                side_effect=[critical, critical],
            ),
            mock.patch.object(
                production_deploy,
                "_prune_build_cache",
                return_value={"keep_storage": "0B"},
            ) as prune,
        ):
            with self.assertRaisesRegex(
                production_deploy.DeploymentError,
                "insufficient_disk_capacity_after_deploy_cleanup",
            ):
                production_deploy._prune_build_cache_for_capacity(
                    failure_reason="insufficient_disk_capacity_after_deploy_cleanup"
                )
        prune.assert_called_once_with(pressure=True)

    def test_transient_backup_image_cleanup_is_idempotent_and_running_safe(self) -> None:
        reference = f"{production_deploy.BACKUP_IMAGE}:latest"
        with (
            mock.patch.object(production_deploy, "_image_id", return_value=""),
            mock.patch.object(production_deploy, "_running_image_ids") as running,
            mock.patch.object(production_deploy, "_run") as run,
        ):
            missing = production_deploy._remove_transient_backup_image()
        self.assertEqual(missing, {"reference": reference, "present": False, "removed": False})
        running.assert_not_called()
        run.assert_not_called()

        image_id = "sha256:" + "1" * 64
        with (
            mock.patch.object(production_deploy, "_image_id", return_value=image_id),
            mock.patch.object(production_deploy, "_running_image_ids", return_value={image_id}),
            mock.patch.object(production_deploy, "_run") as run,
        ):
            protected = production_deploy._remove_transient_backup_image()
        self.assertEqual(protected["reason"], "running_image")
        self.assertFalse(protected["removed"])
        run.assert_not_called()

        with (
            mock.patch.object(production_deploy, "_image_id", return_value=image_id),
            mock.patch.object(production_deploy, "_running_image_ids", return_value=set()),
            mock.patch.object(production_deploy, "_run") as run,
        ):
            removed = production_deploy._remove_transient_backup_image()
        self.assertTrue(removed["removed"])
        run.assert_called_once_with(["docker", "image", "rm", reference])


    def test_cleanup_after_encrypted_backup_removes_transient_image_before_rollout(self) -> None:
        before = {
            "total_bytes": 30 * 1024**3,
            "used_bytes": 23 * 1024**3,
            "free_bytes": 7 * 1024**3,
            "used_percent": 76.0,
        }
        after = {
            "total_bytes": 30 * 1024**3,
            "used_bytes": 20 * 1024**3,
            "free_bytes": 10 * 1024**3,
            "used_percent": 66.7,
        }
        with (
            mock.patch.object(production_deploy, "_disk_capacity", return_value=before),
            mock.patch.object(
                production_deploy,
                "_remove_transient_backup_image",
                return_value={
                    "reference": f"{production_deploy.BACKUP_IMAGE}:latest",
                    "present": True,
                    "removed": True,
                },
            ) as image_cleanup,
            mock.patch.object(
                production_deploy,
                "_prune_build_cache_for_capacity",
                return_value={
                    "mode": "pressure_full",
                    "keep_storage": "0B",
                    "before_cleanup": before,
                    "after_cleanup": after,
                    "pressure_cleanup_applied": True,
                },
            ) as cache_cleanup,
        ):
            result = production_deploy._cleanup_after_encrypted_backup()

        image_cleanup.assert_called_once_with()
        cache_cleanup.assert_called_once_with()
        self.assertTrue(result["transient_backup_image"]["removed"])
        self.assertEqual(result["disk_before_cleanup"], before)
        self.assertEqual(result["disk_after_cleanup"], after)
        self.assertTrue(result["capacity_ready"])

    def test_post_deploy_retention_records_capacity_and_safe_cleanup(self) -> None:
        before = {
            "total_bytes": 30 * 1024**3,
            "used_bytes": 24 * 1024**3,
            "free_bytes": 6 * 1024**3,
            "used_percent": 80.0,
        }
        after = {
            "total_bytes": 30 * 1024**3,
            "used_bytes": 21 * 1024**3,
            "free_bytes": 9 * 1024**3,
            "used_percent": 70.0,
        }
        with (
            mock.patch.object(production_deploy, "_disk_capacity", return_value=before),
            mock.patch.object(
                production_deploy,
                "_prune_deploy_image_history",
                return_value={
                    "removed_tags": 2,
                    "app_rollbacks_retained_before_deploy": 1,
                    "visual_rollbacks_retained_before_deploy": 1,
                },
            ) as image_retention,
            mock.patch.object(
                production_deploy,
                "_remove_transient_backup_image",
                return_value={
                    "reference": f"{production_deploy.BACKUP_IMAGE}:latest",
                    "present": True,
                    "removed": True,
                },
            ) as transient_cleanup,
            mock.patch.object(
                production_deploy,
                "_prune_build_cache_for_capacity",
                return_value={
                    "mode": "pressure_full",
                    "keep_storage": "0B",
                    "before_cleanup": before,
                    "after_cleanup": after,
                    "pressure_cleanup_applied": True,
                },
            ) as cache_cleanup,
        ):
            result = production_deploy._post_deploy_retention("f" * 40)

        image_retention.assert_called_once_with("f" * 40)
        transient_cleanup.assert_called_once_with()
        cache_cleanup.assert_called_once_with(
            failure_reason="insufficient_disk_capacity_after_deploy_cleanup"
        )
        self.assertEqual(result["disk_before_cleanup"], before)
        self.assertEqual(result["disk_after_cleanup"], after)
        self.assertTrue(result["capacity_ready"])
        self.assertTrue(result["transient_backup_image"]["removed"])
        self.assertEqual(result["build_cache_retention"]["mode"], "pressure_full")
        self.assertEqual(result["image_retention"]["app_rollbacks_retained_before_deploy"], 1)
        self.assertEqual(result["image_retention"]["visual_rollbacks_retained_before_deploy"], 1)

    def test_existing_unready_production_aborts_before_any_container_change(self) -> None:
        compose = ["docker", "compose", "--env-file", "clientplatform.env"]
        with (
            mock.patch.object(production_deploy.os, "geteuid", return_value=0),
            mock.patch.object(production_deploy, "_assert_tracked_worktree_clean"),
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

        post_retention_payload = {
            "image_retention": {"removed_tags": 1},
            "transient_backup_image": {"removed": True},
            "build_cache_retention": {"mode": "bounded", "keep_storage": "2GB"},
            "disk_before_cleanup": {
                "total_bytes": 30 * 1024**3,
                "used_bytes": 18 * 1024**3,
                "free_bytes": 12 * 1024**3,
                "used_percent": 60.0,
            },
            "disk_after_cleanup": {
                "total_bytes": 30 * 1024**3,
                "used_bytes": 18 * 1024**3,
                "free_bytes": 12 * 1024**3,
                "used_percent": 60.0,
            },
            "capacity_ready": True,
        }
        predeploy_backup_patcher = mock.patch.object(
            production_deploy,
            "_remove_transient_backup_image",
            return_value={
                "reference": f"{production_deploy.BACKUP_IMAGE}:latest",
                "present": True,
                "removed": True,
            },
        )
        predeploy_backup_cleanup = predeploy_backup_patcher.start()
        self.addCleanup(predeploy_backup_patcher.stop)

        post_retention_patcher = mock.patch.object(
            production_deploy,
            "_post_deploy_retention",
            return_value=post_retention_payload,
        )
        post_retention = post_retention_patcher.start()
        self.addCleanup(post_retention_patcher.stop)

        with (
            mock.patch.object(production_deploy.os, "geteuid", return_value=0),
            mock.patch.object(production_deploy, "_assert_tracked_worktree_clean"),
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
                "_prune_deploy_image_history",
                return_value={
                    "removed_tags": 7,
                    "app_rollbacks_retained_before_deploy": 1,
                    "visual_rollbacks_retained_before_deploy": 1,
                },
            ) as image_retention,
            mock.patch.object(
                production_deploy,
                "_prune_build_cache",
                return_value={"keep_storage": "2GB"},
            ) as cache_retention,
            mock.patch.object(
                production_deploy,
                "_disk_capacity",
                return_value={
                    "total_bytes": 30 * 1024**3,
                    "used_bytes": 18 * 1024**3,
                    "free_bytes": 12 * 1024**3,
                    "used_percent": 60.0,
                },
            ),
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
        image_retention.assert_called_once_with("a" * 40)
        predeploy_backup_cleanup.assert_called_once_with()
        cache_retention.assert_called_once_with()
        post_retention.assert_called_once_with("a" * 40)
        rollback.assert_not_called()
        self.assertEqual(evidence_payload["image_retention"]["removed_tags"], 7)
        self.assertTrue(evidence_payload["predeploy_backup_image_retention"]["removed"])
        self.assertEqual(evidence_payload["build_cache_retention"]["keep_storage"], "2GB")
        self.assertEqual(evidence_payload["disk_before_deploy"]["used_percent"], 60.0)
        self.assertEqual(evidence_payload["disk_after_deploy"]["free_bytes"], 12 * 1024**3)
        self.assertTrue(evidence_payload["post_deploy_retention"]["capacity_ready"])
        self.assertTrue(
            evidence_payload["post_deploy_retention"]["transient_backup_image"]["removed"]
        )
        self.assertEqual(
            evidence_payload["post_deploy_retention"]["build_cache_retention"]["mode"],
            "bounded",
        )
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
        rollback_tag_index = source.index(
            '_run(["docker", "image", "tag", previous_image, rollback_tag])',
            baseline_index,
        )
        retention_index = source.index(
            "image_retention = _prune_deploy_image_history(target_sha)",
            rollback_tag_index,
        )
        predeploy_backup_cleanup_index = source.index(
            "predeploy_backup_image_retention = _remove_transient_backup_image()",
            retention_index,
        )
        cache_retention_index = source.index(
            "build_cache_retention = _prune_build_cache_for_capacity()",
            predeploy_backup_cleanup_index,
        )
        disk_guard_index = source.index(
            'disk_before_deploy = build_cache_retention["after_cleanup"].copy()'
        )
        backup_index = source.index("backup_reference =")
        backup_cleanup_index = source.index(
            "backup_artifact_retention = _cleanup_after_encrypted_backup()",
            backup_index,
        )
        gateway_build_index = source.index('_run([*compose, "build", "visual-gateway"])')
        build_index = source.index('_run([*compose, "build", "app"])')
        gateway_recreate_index = source.index(
            '"--force-recreate", "visual-gateway"',
            gateway_build_index,
        )
        recreate_index = source.index(
            '"--force-recreate", "app", "caddy"',
            build_index,
        )
        post_retention_index = source.index(
            "post_deploy_retention = _post_deploy_retention(target_sha)"
        )

        self.assertLess(baseline_index, rollback_tag_index)
        self.assertLess(rollback_tag_index, retention_index)
        self.assertLess(retention_index, predeploy_backup_cleanup_index)
        self.assertLess(predeploy_backup_cleanup_index, cache_retention_index)
        self.assertLess(cache_retention_index, disk_guard_index)
        self.assertLess(disk_guard_index, backup_index)
        self.assertLess(backup_index, backup_cleanup_index)
        self.assertLess(backup_cleanup_index, gateway_build_index)
        self.assertLess(gateway_build_index, build_index)
        self.assertLess(build_index, gateway_recreate_index)
        self.assertLess(gateway_recreate_index, recreate_index)
        self.assertLess(recreate_index, post_retention_index)
        self.assertNotIn('"docker", "volume", "prune"', source)
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

    def test_production_image_uses_official_postgres_without_retired_bundled_audio(self) -> None:
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
        self.assertIn("clientplatform_sales_production_smoke.py", dockerfile)
        self.assertNotIn("CLIENTPLATFORM_REQUIRE_AUDIO_ASSETS", dockerfile)
        self.assertNotIn("clientplatform_container_audio_assets", dockerfile)
        self.assertNotIn("apt.postgresql.org", dockerfile)
        self.assertNotIn("www.postgresql.org", dockerfile)
        self.assertNotIn("ACCC4CF8", dockerfile)
        self.assertNotIn("postgresql-client-16", dockerfile)

    def test_compose_has_no_retired_bundled_audio_build_switch(self) -> None:
        compose = (
            Path(production_deploy.ROOT) / "deploy/clientplatform/compose.production.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("CLIENTPLATFORM_REQUIRE_AUDIO_ASSETS", compose)

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
            "clientplatform_program_media_preflight",
            "clientplatform_bot_gateway_preflight",
        ):
            self.assertIn(f"python -m scripts.{module}", entrypoint)
            self.assertIn(f"/app/scripts/{module}.py", dockerfile)
        self.assertIn("exec python main.py", entrypoint)
        self.assertNotIn('entrypoint: ["/bin/sh", "-ec"]', compose)
        self.assertNotIn("python scripts/clientplatform_production_preflight.py", compose)
        self.assertNotIn("python scripts/clientplatform_program_media_preflight.py", compose)
        self.assertNotIn("python scripts/clientplatform_bot_gateway_preflight.py", compose)


if __name__ == "__main__":
    unittest.main()
