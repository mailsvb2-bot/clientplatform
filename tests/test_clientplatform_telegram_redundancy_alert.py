from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


_AIOGRAM_AVAILABLE = importlib.util.find_spec("aiogram") is not None
_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "clientplatform"
    / "runtime"
    / "admin_observability.py"
)


@unittest.skipUnless(_AIOGRAM_AVAILABLE, "aiogram is not installed")
class TelegramRedundancyAlertRuntimeTests(unittest.TestCase):
    @staticmethod
    def _helper(*, redundant: bool, required: bool) -> bool | None:
        from clientplatform.runtime.admin_observability import (
            _route_redundancy_alert_state,
        )

        return _route_redundancy_alert_state(
            egress_redundant=redundant,
            redundancy_required=required,
        )

    def test_optional_single_route_is_informational(self) -> None:
        self.assertIsNone(self._helper(redundant=False, required=False))

    def test_required_single_route_creates_alert_state(self) -> None:
        self.assertFalse(self._helper(redundant=False, required=True))

    def test_required_redundant_route_clears_alert_state(self) -> None:
        self.assertTrue(self._helper(redundant=True, required=True))


class TelegramRedundancyAlertSourceContractTests(unittest.TestCase):
    def test_tick_applies_operator_requirement_before_business_alert(self) -> None:
        source = _SOURCE.read_text(encoding="utf-8")
        self.assertIn("telegram_redundancy_required()", source)
        self.assertIn("_route_redundancy_alert_state", source)
        self.assertIn("route_redundant=route_redundant", source)
        self.assertNotIn(
            "route_redundant=telegram_egress_snapshot().egress_redundant",
            source,
        )


if __name__ == "__main__":
    unittest.main()
