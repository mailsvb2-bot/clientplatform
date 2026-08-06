from __future__ import annotations

import unittest
from pathlib import Path


class YandexScreenCodeHandlerOrderTests(unittest.TestCase):
    def test_screen_code_handler_is_the_only_connect_callback_route(self) -> None:
        root = Path(__file__).resolve().parents[1]
        screen_code_source = (
            root / "handlers/clientplatform_yandex_screen_code.py"
        ).read_text(encoding="utf-8")
        legacy_source = (
            root / "handlers/clientplatform_ad_connections.py"
        ).read_text(encoding="utf-8")
        route = '@simple.router.callback_query(F.data.startswith("cpa:connect:"))'
        self.assertEqual(screen_code_source.count(route), 1)
        self.assertNotIn(route, legacy_source)

        package_source = (root / "handlers/__init__.py").read_text(encoding="utf-8")
        self.assertIn('".clientplatform_yandex_screen_code"', package_source)
        self.assertIn('".clientplatform_ad_connections"', package_source)

    def test_complete_oauth_production_surface_is_security_scanned(self) -> None:
        root = Path(__file__).resolve().parents[1]
        gate_source = (root / "scripts/critical_static_gate.py").read_text(
            encoding="utf-8"
        )
        required_paths = (
            "clientplatform/application/ad_oauth_sessions.py",
            "clientplatform/infrastructure/ad_oauth_session_store.py",
            "clientplatform/integrations/yandex_screen_code.py",
            "handlers/clientplatform_yandex_screen_code.py",
            "runtime/ad_oauth_http.py",
            "scripts/clientplatform_ad_connections_preflight.py",
            "scripts/clientplatform_prepare_production_env.py",
        )
        for relative in required_paths:
            with self.subTest(path=relative):
                self.assertIn(f'"{relative}"', gate_source)

    def test_screen_code_redirect_contract_cannot_drift_between_layers(self) -> None:
        root = Path(__file__).resolve().parents[1]
        redirect_uri = "https://oauth.yandex.ru/verification_code"
        contract_paths = (
            "clientplatform/integrations/yandex_screen_code.py",
            "runtime/ad_oauth_http.py",
            "scripts/clientplatform_ad_connections_preflight.py",
            "scripts/clientplatform_prepare_production_env.py",
        )
        for relative in contract_paths:
            with self.subTest(path=relative):
                source = (root / relative).read_text(encoding="utf-8")
                self.assertIn(redirect_uri, source)


if __name__ == "__main__":
    unittest.main()
