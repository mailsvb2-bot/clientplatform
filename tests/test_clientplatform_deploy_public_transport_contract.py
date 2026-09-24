from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from scripts import clientplatform_production_deploy as production_deploy


class DeployPublicTransportContractTests(unittest.TestCase):
    def test_public_business_page_is_routed_to_app_before_media_fallback(self) -> None:
        source = Path("deploy/clientplatform/Caddyfile").read_text(encoding="utf-8")
        route_position = source.index("/clientplatform/b/*")
        media_position = source.index("@media path /clientplatform/*")
        self.assertLess(route_position, media_position)
        ingress_block_start = source.rfind(
            "@clientplatform_public_business",
            0,
            route_position + 1,
        )
        ingress_block_end = source.index("}", route_position)
        ingress_block = source[ingress_block_start:ingress_block_end]
        self.assertIn("reverse_proxy {$CLIENTPLATFORM_INGRESS_UPSTREAM", ingress_block)

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

    def test_env_flag_enabled_is_explicit_and_fail_closed(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(
                    production_deploy._env_flag_enabled(
                        {"CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": value},
                        "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED",
                    )
                )
        for value in ("", "0", "false", "off", "unexpected"):
            with self.subTest(value=value):
                self.assertFalse(
                    production_deploy._env_flag_enabled(
                        {"CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED": value},
                        "CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED",
                    )
                )

    def test_canonical_omnichannel_method_guards_are_inert_gets(self) -> None:
        success = subprocess.CompletedProcess(
            args=["curl"], returncode=0,
            stdout="HTTP/2 405\r\nallow: POST\r\ncontent-length: 0\r\n\r\n", stderr="",
        )
        with mock.patch.object(
            production_deploy, "_run", side_effect=[success, success]
        ) as run:
            production_deploy._external_omnichannel_method_guards(
                "clientplatform.example.test"
            )
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][-1], "https://clientplatform.example.test/clientplatform/webhooks/vk/production-deploy-probe")
        self.assertEqual(commands[1][-1], "https://clientplatform.example.test/clientplatform/webhooks/max/production-deploy-probe")
        for command in commands:
            self.assertIn("GET", command)
            self.assertNotIn("POST", command)
            self.assertNotIn("--data-binary", command)

    def test_canonical_omnichannel_method_guard_fails_closed(self) -> None:
        bad_responses = (
            subprocess.CompletedProcess(args=["curl"], returncode=0, stdout="HTTP/2 404\r\ncontent-length: 0\r\n\r\n", stderr=""),
            subprocess.CompletedProcess(args=["curl"], returncode=0, stdout="HTTP/2 405\r\nallow: GET\r\n\r\n", stderr=""),
            subprocess.CompletedProcess(args=["curl"], returncode=28, stdout="", stderr="timeout"),
        )
        for response in bad_responses:
            with self.subTest(returncode=response.returncode, stdout=response.stdout):
                with mock.patch.object(production_deploy, "_run", return_value=response):
                    with self.assertRaisesRegex(production_deploy.DeploymentError, "external_canonical_vk_webhook_guard_failed"):
                        production_deploy._external_post_only_guard(
                            "clientplatform.example.test",
                            "/clientplatform/webhooks/vk/production-deploy-probe",
                            failure_reason="external_canonical_vk_webhook_guard_failed",
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
        current_deploy_gate = source[deploy_gate_start:deploy_gate_end]
        self.assertIn("_external_https(domain)", current_deploy_gate)
        self.assertEqual(
            current_deploy_gate.count("_external_omnichannel_method_guards(domain)"),
            2,
        )
        self.assertIn("if omnichannel_enabled:", current_deploy_gate)

        rollback_start = source.index("def _rollback(")
        rollback_end = source.index("def deploy(", rollback_start)
        rollback = source[rollback_start:rollback_end]
        self.assertIn("_external_https(domain)", rollback)
        self.assertNotIn("_external_omnichannel_method_guards(domain)", rollback)

    def test_deploy_evidence_records_polling_contract(self) -> None:
        source = Path(production_deploy.__file__).read_text(encoding="utf-8")
        self.assertIn('"telegram_transport": "polling"', source)
        self.assertIn('"telegram_webhook_prefix": webhook_prefix', source)
        self.assertIn('"telegram_webhook_absent": True', source)
        self.assertIn('"canonical_omnichannel_ingress_enabled": omnichannel_enabled', source)
        self.assertIn('"canonical_omnichannel_routes_proven": omnichannel_enabled', source)
        self.assertIn("external_telegram_webhook_absence_failed", source)


if __name__ == "__main__":
    unittest.main()
