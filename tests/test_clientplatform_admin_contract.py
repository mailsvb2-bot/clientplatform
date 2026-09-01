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

    def test_admin_is_a_tenant_safe_grouped_business_panel(self) -> None:
        text = ADMIN.read_text(encoding="utf-8")
        tree = ast.parse(text)
        functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        for required in {
            "_load_admin_context",
            "_menu_keyboard",
            "_safe_edit",
            "_navigate_back",
            "_assert_section_allowed",
            "_render_customer_card",
            "_render_members",
            "_render_permissions",
            "admin_gate",
            "open_admin_command",
            "install_admin_dashboard_button",
        }:
            self.assertIn(required, functions)

        self.assertIn("⚙️ Управление бизнесом", text)
        self.assertIn('text="🛠 Панель"', text)
        self.assertIn("owner_navigation as nav", text)
        self.assertIn("_ADMIN_GROUP_NEEDS", text)
        self.assertIn("_ADMIN_ACTION_NEEDS", text)
        self.assertIn("Если Вам нужно:", text)
        self.assertIn("_admin_group_items", text)
        self.assertIn("_render_admin_group", text)
        for action in (
            "today",
            "today-full",
            "customers",
            "customer-list",
            "behavior",
            "messengers",
            "attention",
            "autopilot",
            "publications",
            "funnel",
            "money",
            "payments",
            "segments",
            "offers",
            "copy",
            "prices",
            "release",
            "invites",
            "funnel2",
            "retention",
            "recent",
            "system",
            "tariff",
            "add-member",
            "members",
            "permissions",
        ):
            self.assertIn(f'"{action}"', text)
        self.assertIn('"⬅️ Назад"', text)

    def test_admin_never_routes_into_legacy_global_clientplatform_admin(self) -> None:
        text = ADMIN.read_text(encoding="utf-8")

        self.assertNotIn("ADMIN_IDS", text)
        self.assertIn("business_id", text)
        self.assertIn("control._actor", text)
        self.assertIn("TenancyRepository", text)
        self.assertIn("TenantPermissionDenied", text)

    def test_every_admin_callback_revalidates_live_role(self) -> None:
        text = ADMIN.read_text(encoding="utf-8")

        self.assertIn('@router.callback_query(F.data.startswith("cpa:"))', text)
        self.assertIn("ctx = await _load_admin_context(", text)
        self.assertIn("_assert_section_allowed(ctx, action)", text)
        self.assertIn("resolve_context(", text)
        self.assertIn("current.assert_can_manage_business()", text)

    def test_admin_callback_payloads_stay_in_clientplatform_namespace(self) -> None:
        text = ADMIN.read_text(encoding="utf-8")

        self.assertIn('value = f"cpa:{ctx.business_token}:{action}"', text)
        self.assertIn('raise ValueError("ClientPlatform admin callback exceeds Telegram limit")', text)
        self.assertNotIn('callback_data="admin:', text)

    def test_callbacks_are_acknowledged_before_single_flight_lock(self) -> None:
        text = SAFETY.read_text(encoding="utf-8")
        acknowledgement = "await _answer_callback(event)"
        lock = "async with lock:"

        self.assertIn(acknowledgement, text)
        self.assertIn(lock, text)
        self.assertLess(text.index(acknowledgement), text.index(lock))
        self.assertIn("_optimized_dashboard_queries_installed", text)
        self.assertIn('"/admin"', text)


if __name__ == "__main__":
    unittest.main()
