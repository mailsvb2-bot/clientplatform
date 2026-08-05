from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


_AIOGRAM_AVAILABLE = importlib.util.find_spec("aiogram") is not None
_SOURCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "clientplatform"
    / "presentation"
    / "ad_spend_telegram.py"
)


@unittest.skipUnless(_AIOGRAM_AVAILABLE, "aiogram is not installed")
class AdSpendTelegramRuntimeTests(unittest.TestCase):
    @staticmethod
    def _module():
        from clientplatform.presentation import ad_spend_telegram

        return ad_spend_telegram

    def test_parse_minor_units_is_exact(self) -> None:
        module = self._module()
        cases = (
            ("1", 100),
            ("1,25", 125),
            ("500.00", 50_000),
            (" 12 345,67 ", 1_234_567),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(module._parse_minor_units(raw), expected)

    def test_parse_minor_units_rejects_ambiguous_or_unsafe_values(self) -> None:
        module = self._module()
        for raw in ("", "0", "-1", "nan", "inf", "1.001", "not-money"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    module._parse_minor_units(raw)

    def test_format_minor_units_keeps_currency_visible(self) -> None:
        module = self._module()
        self.assertEqual(module._format_minor(12_345, "RUB"), "123,45 RUB")


class AdSpendTelegramSourceContractTests(unittest.TestCase):
    def test_consent_copy_does_not_claim_that_spend_already_started(self) -> None:
        source = _SOURCE_PATH.read_text(encoding="utf-8")
        self.assertIn("Показы и расходы не запущены", source)
        self.assertIn("Подтверждение создания", source)
        self.assertIn("черновика DRAFT никогда не считается согласием", source)
        self.assertIn("идемпотентная очередь запуска и остановки", source)
        self.assertNotIn("provider.publish", source)
        self.assertNotIn("moderate(", source)
        self.assertNotIn("suspend(", source)


if __name__ == "__main__":
    unittest.main()
