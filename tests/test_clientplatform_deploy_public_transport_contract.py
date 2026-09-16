from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from scripts import clientplatform_production_deploy as production_deploy


class DeployPublicTransportContractTests(unittest.TestCase):
    def test_webhook_prefix_defaults_and_rejects_unsafe_values(self) -> None:
        self.assertEqual(
            production_deploy._telegram_webhook_prefix({}),
            "/telegram-webhook",
        )
        self.assertEqual(
            production_deploy._telegram_webhook_prefix(
                {"TELEGRAM_WEBHOOK_PREFIX": "/internal/telegram-v2"}
            ),
            "/internal/telegram-v2",
        )

        invalid_values = (
            "telegram-webhook",
            "//telegram-webhook",
            "/",
            "/telegram webhook",
            "/telegram-webhook?token=value",
            "/telegram-webhook#fragment",
            "/telegram\\webhook",
            "/" + "x" * 256,
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    production_deploy.DeploymentError,
                    "invalid_telegram_webhook_prefix",
                ):
                    production_deploy._telegram_webhook_prefix(
                        {"TELEGRAM_WEBHOOK_PREFIX": value}
                    )

    def test_external_root_requires_exact_public_brand(self) -> None:
        success = subprocess.CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout="ClientPlatform\n",
            stderr="",
        )
        with mock.patch.object(
            production_deploy,
            "_run",
            return_value=success,
        ) as run:
            production_deploy._external_root("clientplatform.example.test")

        command = run.call_args.args[0]
        self.assertEqual(command[-1], "https://clientplatform.example.test/")
        self.assertIn("--proto", command)
        self.assertIn("=https", command)
        self.assertIn("--tlsv1.2", command)

        wrong_body = subprocess.CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout="unexpected",
            stderr="",
        )
        with mock.patch.object(
            production_deploy,
            "_run",
            return_value=wrong_body,
        ):
            with self.assertRaisesRegex(
                production_deploy.DeploymentError,
                "external_https_proof_failed",
            ):
                production_deploy._external_root("clientplatform.example.test")

    def test_polling_absence_requires_404_and_uses_inert_post(self) -> None:
        not_found = subprocess.CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout="404",
            stderr="",
        )
        with mock.patch.object(
            production_deploy,
            "_run",
            return_value=not_found,
        ) as run:
            production_deploy._external_polling_absence(
                "clientplatform.example.test",
                "/telegram-private",
            )

        command = run.call_args.args[0]
        self.assertEqual(
            command[-1],
            "https://clientplatform.example.test/telegram-private",
        )
        self.assertIn("POST", command)
        self.assertIn("{}", command)
        self.assertIn(
            "X-Telegram-Bot-Api-Secret-Token: intentionally-invalid-deploy-proof",
            command,
        )
        self.assertIn("%{http_code}", command)

        for status in ("200", "400", "401", "403", "500"):
            with self.subTest(status=status):
                response = subprocess.CompletedProcess(
                    args=["curl"],
                    returncode=0,
                    stdout=status,
                    stderr="",
                )
                with mock.patch.object(
                    production_deploy,
                    "_run",
                    return_value=response,
                ):
                    with self.assertRaisesRegex(
                        production_deploy.DeploymentError,
                        "external_telegram_webhook_absence_failed",
                    ):
                        production_deploy._external_polling_absence(
                            "clientplatform.example.test",
                            "/telegram-webhook",
                        )

    def test_full_external_contract_orders_root_before_absence(self) -> None:
        calls: list[tuple[str, object]] = []
        with (
            mock.patch.object(
                production_deploy,
                "_external_root",
                side_effect=lambda domain: calls.append(("root", domain)),
            ),
            mock.patch.object(
                production_deploy,
                "_telegram_webhook_prefix",
                return_value="/telegram-webhook",
            ),
            mock.patch.object(
                production_deploy,
                "_external_polling_absence",
                side_effect=lambda domain, prefix: calls.append(
                    ("absence", (domain, prefix))
                ),
            ),
        ):
            production_deploy._external_https("clientplatform.example.test")

        self.assertEqual(
            calls,
            [
                ("root", "clientplatform.example.test"),
                (
                    "absence",
                    ("clientplatform.example.test", "/telegram-webhook"),
                ),
            ],
        )

    def test_baseline_is_upgrade_compatible_but_new_and_rollback_are_strict(self) -> None:
        source = Path(production_deploy.__file__).read_text(encoding="utf-8")
        baseline_start = source.index("if app_exists:")
        baseline_end = source.index(
            '_run([*compose, "up", "-d", "postgres"])',
            baseline_start,
        )
        baseline = source[baseline_start:baseline_end]
        self.assertIn("_external_root(domain)", baseline)
        self.assertNotIn("_external_https(domain)", baseline)

        deploy_gate_start = source.index("changed = False")
        deploy_gate_end = source.index(
            "except Exception as deployment_error",
            deploy_gate_start,
        )
        self.assertIn(
            "_external_https(domain)",
            source[deploy_gate_start:deploy_gate_end],
        )

        rollback_start = source.index("def _rollback(")
        rollback_end = source.index("def deploy(", rollback_start)
        self.assertIn(
            "_external_https(domain)",
            source[rollback_start:rollback_end],
        )

    def test_deploy_evidence_records_polling_contract(self) -> None:
        source = Path(production_deploy.__file__).read_text(encoding="utf-8")
        self.assertIn('"telegram_transport": "polling"', source)
        self.assertIn('"telegram_webhook_prefix": webhook_prefix', source)
        self.assertIn('"telegram_webhook_absent": True', source)
        self.assertIn("external_telegram_webhook_absence_failed", source)


if __name__ == "__main__":
    unittest.main()
