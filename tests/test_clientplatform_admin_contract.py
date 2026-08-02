from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "handlers" / "clientplatform_entry.py"
ADMIN = ROOT / "handlers" / "clientplatform_admin.py"
SAFETY = ROOT / "handlers" / "clientplatform_interaction_safety.py"


class ClientPlatformAdminContractTests(unittest.TestCase):
    def test_admin_command_and_router_are_composed_before_legacy_handlers(self) -> None:
        text = ENTRY.read_text(encoding="utf-8")
        admin = "router.include_router(admin.router)"
        original = "router.include_router(original_router)"

        self.assertIn('BotCommand(command="admin"', text)
        self.assertIn('@router.message(Command("admin"))', text)
        self.assertIn("admin.install_admin_dashboard_button(control)", text)
        self.assertIn(admin, text)
        self.assertLess(text.index(admin), text.index(original))
        self.assertIn("control._admin_router_composed = True", text)

    def test_admin_module_exposes_real_owner_operations(self) -> None:
        text = ADMIN.read_text(encoding="utf-8")
        tree = ast.parse(text)
        functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertIn("send_admin_panel", functions)
        self.assertIn("open_admin_command", functions)
        self.assertIn("install_admin_dashboard_button", functions)
        self.assertIn('text="Админка"', text)
        self.assertIn('"cpa:home:', text)
        self.assertIn('"cpa:formats:', text)
        self.assertIn('"cpa:back:', text)
        self.assertIn("business_delivery_summary", text)
        self.assertIn("list_booking_slots", text)

    def test_callbacks_are_acknowledged_before_single_flight_lock(self) -> None:
        text = SAFETY.read_text(encoding="utf-8")
        acknowledgement = "await _answer_callback(event)"
        lock = "async with lock:"
        load = "async def load_dashboard_context"

        self.assertIn(acknowledgement, text)
        self.assertIn(lock, text)
        self.assertLess(text.index(acknowledgement), text.index(lock))
        self.assertIn(load, text)
        self.assertIn("await asyncio.gather(", text)
        self.assertIn("_optimized_dashboard_queries_installed", text)
        self.assertIn('"cpa:home:"', text)


if __name__ == "__main__":
    unittest.main()
