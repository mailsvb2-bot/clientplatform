from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from scripts import clientplatform_production_deploy as deploy


def _completed(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["docker", "exec"],
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )


class ProductionReadinessDiagnosticTests(unittest.TestCase):
    def test_readiness_diagnostic_is_strictly_allowlisted(self) -> None:
        provider_payload = {
            "available": True,
            "http_status": 500,
            "db_ready": True,
            "schema_ready": True,
            "clientplatform_dispatch_ready": False,
            "clientplatform_dispatch_errors": 2,
            "max_preflight_missing": ["MAX_WEBHOOK_SECRET"],
            "clientplatform_dispatch_last_error": "token=must-not-leak",
            "secret": "must-not-leak",
        }
        with mock.patch.object(
            deploy,
            "_run",
            return_value=_completed(provider_payload),
        ) as run:
            result = deploy._readiness_diagnostic()

        self.assertEqual(
            result,
            {
                "available": True,
                "http_status": 500,
                "db_ready": True,
                "schema_ready": True,
                "clientplatform_dispatch_ready": False,
                "clientplatform_dispatch_errors": 2,
                "max_preflight_missing": ["MAX_WEBHOOK_SECRET"],
            },
        )
        command = run.call_args.args[0]
        self.assertEqual(
            command[:3],
            ["docker", "exec", deploy.APP_CONTAINER],
        )
        self.assertIn("HEALTHCHECK_DIAGNOSTICS_TOKEN", command[-1])
        self.assertNotIn("must-not-leak", json.dumps(result))

    def test_readiness_diagnostic_rejects_invalid_or_failed_probe(self) -> None:
        invalid = subprocess.CompletedProcess(
            args=["docker", "exec"],
            returncode=0,
            stdout="not-json",
            stderr="secret",
        )
        failed = subprocess.CompletedProcess(
            args=["docker", "exec"],
            returncode=1,
            stdout='{"available":true,"db_ready":true}',
            stderr="secret",
        )

        with mock.patch.object(deploy, "_run", return_value=invalid):
            self.assertEqual(
                deploy._readiness_diagnostic(),
                {"available": False},
            )
        with mock.patch.object(deploy, "_run", return_value=failed):
            self.assertEqual(
                deploy._readiness_diagnostic(),
                {"available": False},
            )

    def test_readiness_timeout_emits_safe_diagnostic_before_failure(self) -> None:
        with (
            mock.patch.object(deploy.time, "monotonic", side_effect=[0.0, 2.0]),
            mock.patch.object(deploy, "_emit_readiness_diagnostic") as emit,
        ):
            with self.assertRaisesRegex(
                deploy.DeploymentError,
                "production_readiness_timeout",
            ):
                deploy._wait_for_readiness(1)

        emit.assert_called_once_with("readiness_timeout")

    def test_baseline_timeout_emits_safe_diagnostic_before_failure(self) -> None:
        with (
            mock.patch.object(deploy.time, "monotonic", side_effect=[0.0, 2.0]),
            mock.patch.object(deploy, "_emit_readiness_diagnostic") as emit,
        ):
            with self.assertRaisesRegex(
                deploy.DeploymentError,
                "production_baseline_readiness_timeout",
            ):
                deploy._wait_for_baseline_readiness(1)

        emit.assert_called_once_with("baseline_timeout")


if __name__ == "__main__":
    unittest.main()
