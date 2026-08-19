from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_OPERATIONS = _ROOT / "handlers" / "clientplatform_sales_operations.py"
_INSTALL = _ROOT / "handlers" / "clientplatform_sales_install.py"
_HANDLERS_INIT = _ROOT / "handlers" / "__init__.py"


class ClientPlatformSalesOwnerOperationsContractTests(unittest.TestCase):
    def test_owner_surface_calls_every_u008_mutation_boundary(self) -> None:
        source = _OPERATIONS.read_text(encoding="utf-8")
        for operation in (
            "assign_sales_lead",
            "unassign_sales_lead",
            "transition_sales_lead",
            "set_sales_next_action",
            "clear_sales_next_action",
            "add_sales_note",
        ):
            self.assertGreaterEqual(source.count(f"{operation}("), 1, operation)

        for callback_prefix in (
            "cps:swme:",
            "cps:swmu:",
            "cps:swms:",
            "cps:swmn:",
            "cps:swmd:",
            "cps:swmx:",
            "cps:swmo:",
            "cps:swmc:",
            "cps:swmr:",
        ):
            self.assertIn(callback_prefix, source)

    def test_owner_projection_renders_u008_durable_and_attribution_fields(self) -> None:
        source = _OPERATIONS.read_text(encoding="utf-8")
        for field in (
            "assigned_member_id",
            "assigned_user_id",
            "next_action",
            "due_at",
            "closure_reason",
            "source_kind",
            "source_ref",
            "attribution_source",
            "attribution_source_ref_type",
            "attribution_source_ref_id",
            "attribution_promotion_campaign_id",
        ):
            self.assertIn(field, source)

    def test_mutation_router_is_composed_before_existing_sales_router(self) -> None:
        install_source = _INSTALL.read_text(encoding="utf-8")
        init_source = _HANDLERS_INIT.read_text(encoding="utf-8")

        self.assertIn(
            "simple_module.router.include_router(operations.router)",
            install_source,
        )
        install_call = init_source.index(
            "sales_install.install_sales_ui(simple_experience)"
        )
        existing_router = init_source.index(
            "simple_experience.router.include_router(sales.router)"
        )
        self.assertLess(install_call, existing_router)

    def test_sales_home_keeps_existing_ai_surface_while_exposing_mutations(self) -> None:
        source = _OPERATIONS.read_text(encoding="utf-8")
        self.assertIn("🛠 Управлять обращениями", source)
        self.assertIn("🧠 Рекомендации и ИИ", source)
        self.assertIn('f"cps:sw:{business_token}"', source)


if __name__ == "__main__":
    unittest.main()
