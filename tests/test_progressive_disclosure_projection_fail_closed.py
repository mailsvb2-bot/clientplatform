from __future__ import annotations

import unittest
from unittest.mock import patch

from handlers import clientplatform_goal_dashboard as dashboard


class ProgressiveDisclosureProjectionFailClosedTests(unittest.TestCase):
    def test_owner_projection_failure_is_not_treated_as_empty_queue(self) -> None:
        with patch.object(
            dashboard,
            "get_growth_cockpit",
            side_effect=RuntimeError("projection unavailable"),
        ):
            action = dashboard._owner_next_action("actor")

        self.assertEqual(action.action_key, "projection_unavailable")
        self.assertIn("не удалось проверить", action.title.casefold())
        self.assertNotIn("срочных задач сейчас нет", action.reason.casefold())

        with patch.object(dashboard.control, "_uuid_token", return_value="business-token"):
            label, callback = dashboard._primary_action("business-id", action)
        self.assertIn("вручную", label.casefold())
        self.assertEqual(callback, "cpg:period:business-token:7")


if __name__ == "__main__":
    unittest.main()
