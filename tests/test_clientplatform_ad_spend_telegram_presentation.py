from __future__ import annotations

import inspect
import unittest

from clientplatform.presentation import ad_spend_telegram


class AdSpendTelegramPresentationTests(unittest.TestCase):
    def test_parse_minor_units_is_exact(self) -> None:
        cases = (
            ("1", 100),
            ("1,25", 125),
            ("500.00", 50_000),
            (" 12 345,67 ", 1_234_567),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(
                    ad_spend_telegram._parse_minor_units(raw),
                    expected,
                )

    def test_parse_minor_units_rejects_ambiguous_or_unsafe_values(self) -> None:
        for raw in ("", "0", "-1", "nan", "inf", "1.001", "not-money"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    ad_spend_telegram._parse_minor_units(raw)

    def test_format_minor_units_keeps_currency_visible(self) -> None:
        self.assertEqual(
            ad_spend_telegram._format_minor(12_345, "RUB"),
            "123,45 RUB",
        )

    def test_consent_copy_does_not_claim_that_spend_already_started(self) -> None:
        source = inspect.getsource(ad_spend_telegram)
        self.assertIn("Показы и расходы не запущены", source)
        self.assertIn(
            "Подтверждение создания черновика DRAFT никогда не считается согласием",
            source,
        )
        self.assertIn("идемпотентная очередь запуска и остановки", source)


if __name__ == "__main__":
    unittest.main()
