from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


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

    def test_authorized_item_requires_separate_launch_action(self) -> None:
        module = self._module()
        item = SimpleNamespace(
            status=module.AdSpendAuthorizationStatus.AUTHORIZED,
            external_campaign_id="123456",
        )
        rows = module._authorization_action_rows(
            item=item,
            business_token="business",
            authorization_token="authorization",
            launch_enabled=True,
        )
        payloads = [payload for row in rows for _label, payload in row]
        self.assertEqual(
            payloads,
            [
                "cpsp:launch:business:authorization",
                "cpsp:revoke:business:authorization",
            ],
        )

    def test_kill_switch_hides_launch_but_keeps_revoke(self) -> None:
        module = self._module()
        item = SimpleNamespace(
            status=module.AdSpendAuthorizationStatus.AUTHORIZED,
            external_campaign_id="123456",
        )
        rows = module._authorization_action_rows(
            item=item,
            business_token="business",
            authorization_token="authorization",
            launch_enabled=False,
        )
        payloads = [payload for row in rows for _label, payload in row]
        self.assertEqual(payloads, ["cpsp:revoke:business:authorization"])

    def test_active_item_exposes_stop_and_revoke(self) -> None:
        module = self._module()
        item = SimpleNamespace(
            status=module.AdSpendAuthorizationStatus.ACTIVE,
            external_campaign_id="123456",
        )
        rows = module._authorization_action_rows(
            item=item,
            business_token="business",
            authorization_token="authorization",
            launch_enabled=False,
        )
        payloads = [payload for row in rows for _label, payload in row]
        self.assertEqual(
            payloads,
            [
                "cpsp:stop:business:authorization",
                "cpsp:revoke:business:authorization",
            ],
        )


class AdSpendTelegramSourceContractTests(unittest.TestCase):
    def test_consent_copy_requires_separate_launch_and_safe_stop(self) -> None:
        source = _SOURCE_PATH.read_text(encoding="utf-8")
        self.assertIn("Показы и расходы не запущены", source)
        self.assertIn("Подтверждение создания", source)
        self.assertIn("черновика DRAFT никогда не считается согласием", source)
        self.assertIn("Для запуска потребуется отдельная кнопка", source)
        self.assertIn("queue_ad_spend_launch", source)
        self.assertIn("queue_ad_spend_stop", source)
        self.assertIn("операторским kill switch", source)
        self.assertNotIn("provider.publish", source)
        self.assertNotIn("moderate(", source)
        self.assertNotIn("suspend(", source)


if __name__ == "__main__":
    unittest.main()
