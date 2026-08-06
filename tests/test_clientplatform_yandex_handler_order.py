from __future__ import annotations

import unittest
from pathlib import Path


class YandexScreenCodeHandlerOrderTests(unittest.TestCase):
    def test_screen_code_handler_is_registered_before_legacy_callback_handler(self) -> None:
        root = Path(__file__).resolve().parents[1]
        package_source = (root / "handlers/__init__.py").read_text(encoding="utf-8")
        screen_code_index = package_source.index(
            '".clientplatform_yandex_screen_code"'
        )
        legacy_callback_index = package_source.index(
            '".clientplatform_ad_connections"'
        )
        self.assertLess(screen_code_index, legacy_callback_index)

        screen_code_source = (
            root / "handlers/clientplatform_yandex_screen_code.py"
        ).read_text(encoding="utf-8")
        legacy_source = (
            root / "handlers/clientplatform_ad_connections.py"
        ).read_text(encoding="utf-8")
        route = '@simple.router.callback_query(F.data.startswith("cpa:connect:"))'
        self.assertIn(route, screen_code_source)
        self.assertIn(route, legacy_source)


if __name__ == "__main__":
    unittest.main()
