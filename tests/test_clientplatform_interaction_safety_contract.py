from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "handlers" / "clientplatform_entry.py"
SAFETY = ROOT / "handlers" / "clientplatform_interaction_safety.py"
BOOKINGS = ROOT / "clientplatform" / "application" / "bookings.py"
PROGRESS = ROOT / "clientplatform" / "application" / "progress.py"
SAFE_ACTIVITY = (
    ROOT
    / "clientplatform"
    / "infrastructure"
    / "postgres_safe_activity_repository.py"
)


class ClientPlatformInteractionSafetyContractTests(unittest.TestCase):
    def test_commands_are_registered_before_subrouter_composition(self) -> None:
        text = ENTRY.read_text(encoding="utf-8")

        self.assertIn('@router.message(Command("mybot"))', text)
        self.assertIn('@router.message(Command("cancel"))', text)
        self.assertIn('@router.message(F.text.startswith("/"))', text)
        self.assertLess(
            text.index('@router.message(Command("mybot"))'),
            text.index("router.include_router(original_router)"),
        )
        self.assertIn("Команда не была сохранена как данные", text)

    def test_safety_router_precedes_legacy_control_router(self) -> None:
        text = ENTRY.read_text(encoding="utf-8")
        safety = "router.include_router(interaction_safety.router)"
        original = "router.include_router(original_router)"

        self.assertIn(safety, text)
        self.assertLess(text.index(safety), text.index(original))
        self.assertIn("install_interaction_safety(router, control)", text)

    def test_safety_source_has_single_flight_and_stale_keyboard_contracts(self) -> None:
        text = SAFETY.read_text(encoding="utf-8")
        tree = ast.parse(text)
        class_names = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        }

        self.assertIn("ClientPlatformInteractionSafetyMiddleware", class_names)
        self.assertIn("async with lock:", text)
        self.assertIn("edit_reply_markup(reply_markup=None)", text)
        self.assertIn("Действие уже выполняется", text)
        self.assertIn("Сначала завершите текущий шаг", text)
        self.assertIn("Изменить название", text)

    def test_self_customer_access_is_blocked_at_all_public_boundaries(self) -> None:
        activity = SAFE_ACTIVITY.read_text(encoding="utf-8")
        bookings = BOOKINGS.read_text(encoding="utf-8")
        progress = PROGRESS.read_text(encoding="utf-8")

        self.assertIn("_assert_invite_claim_is_external", activity)
        self.assertIn("JOIN business_members", activity)
        self.assertIn("active_member_business_ids", bookings)
        self.assertGreaterEqual(bookings.count("assert_external_customer("), 2)
        self.assertGreaterEqual(progress.count("assert_external_customer("), 3)


if __name__ == "__main__":
    unittest.main()
