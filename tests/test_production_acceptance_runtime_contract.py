from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from scripts import production_acceptance as acceptance


class ProductionAcceptanceRuntimeContractTests(unittest.TestCase):
    def test_container_checks_target_canonical_app_container(self) -> None:
        self.assertEqual(
            acceptance._container_python("scripts/prod_readiness_check.py"),
            [
                "docker",
                "exec",
                acceptance.APP_CONTAINER,
                "python",
                "scripts/prod_readiness_check.py",
            ],
        )

    def test_default_public_origin_is_current_backend_ingress(self) -> None:
        self.assertEqual(
            acceptance.DEFAULT_PUBLIC_BASE_URL,
            "https://app.clientplatform.ru",
        )

    def test_acceptance_no_longer_probes_unpublished_host_ports(self) -> None:
        source = Path(acceptance.__file__).read_text(encoding="utf-8")
        self.assertNotIn("http://127.0.0.1:8181", source)
        self.assertNotIn("http://127.0.0.1:8182", source)
        self.assertNotIn("clientplatform-bot.clientplatform.ru", source)
        self.assertIn("canonical_healthy", source)
        self.assertIn("canonical_ready", source)
        self.assertIn("canonical_runtime_markers", source)
        self.assertIn("canonical_sales_operations_smoke", source)

    def test_collect_keeps_canonical_vk_and_max_public_route_gates(self) -> None:
        ok = acceptance.AcceptanceResult("stub", True, "ok")
        probed: list[str] = []

        def method_probe(name: str, url: str, **_: object) -> acceptance.AcceptanceResult:
            probed.append(url)
            return acceptance.AcceptanceResult(name, True, "ok")

        with (
            mock.patch.object(acceptance, "_run", return_value=ok),
            mock.patch.object(acceptance, "canonical_healthy", return_value=True),
            mock.patch.object(acceptance, "canonical_ready", return_value=True),
            mock.patch.object(acceptance, "canonical_runtime_markers", return_value=True),
            mock.patch.object(acceptance, "_canonical_sales", return_value=ok),
            mock.patch.object(acceptance, "_public_root", return_value=ok),
            mock.patch.object(acceptance, "_method_probe", side_effect=method_probe),
            mock.patch.dict("os.environ", {}, clear=True),
        ):
            results = acceptance.collect_results()

        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(
            probed,
            [
                "https://app.clientplatform.ru/clientplatform/webhooks/vk/production-acceptance-probe",
                "https://app.clientplatform.ru/clientplatform/webhooks/max/production-acceptance-probe",
            ],
        )
        self.assertNotIn("https://app.clientplatform.ru/webhooks/max", probed)


if __name__ == "__main__":
    unittest.main()
