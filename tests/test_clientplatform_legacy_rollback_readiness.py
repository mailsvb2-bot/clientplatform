from __future__ import annotations

from unittest import mock

from scripts import clientplatform_production_deploy as production_deploy


def test_rollback_uses_persistent_readiness_without_new_image_markers() -> None:
    compose = ["docker", "compose", "--env-file", "clientplatform.env"]

    with (
        mock.patch.object(production_deploy, "_run") as run,
        mock.patch.object(
            production_deploy,
            "_wait_for_baseline_readiness",
        ) as persistent_readiness,
        mock.patch.object(production_deploy, "_wait_for_startup") as startup,
        mock.patch.object(production_deploy, "_runtime_markers") as markers,
        mock.patch.object(production_deploy, "_external_https") as external,
    ):
        production_deploy._rollback(
            compose=compose,
            rollback_tag="clientplatform-production-app:rollback-legacy",
            domain="clientplatform.example.test",
            timeout_seconds=120,
        )

    assert run.call_args_list == [
        mock.call(
            [
                "docker",
                "image",
                "tag",
                "clientplatform-production-app:rollback-legacy",
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
    ]
    persistent_readiness.assert_called_once_with(120)
    external.assert_called_once_with("clientplatform.example.test")
    startup.assert_not_called()
    markers.assert_not_called()


def test_rollback_still_fails_closed_when_persistent_readiness_fails() -> None:
    with (
        mock.patch.object(production_deploy, "_run"),
        mock.patch.object(
            production_deploy,
            "_wait_for_baseline_readiness",
            side_effect=production_deploy.DeploymentError("legacy-not-ready"),
        ),
        mock.patch.object(production_deploy, "_external_https") as external,
    ):
        try:
            production_deploy._rollback(
                compose=["docker", "compose"],
                rollback_tag="clientplatform-production-app:rollback-legacy",
                domain="clientplatform.example.test",
                timeout_seconds=60,
            )
        except production_deploy.DeploymentError as exc:
            assert str(exc) == "rollback_not_available"
        else:  # pragma: no cover - regression guard
            raise AssertionError("rollback must fail closed")

    external.assert_not_called()
