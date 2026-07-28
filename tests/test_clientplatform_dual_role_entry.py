from __future__ import annotations

import ast
import unittest
from pathlib import Path


class ClientPlatformDualRoleEntryContractTests(unittest.TestCase):
    def test_entry_surface_is_dependency_light_to_verify(self) -> None:
        source = Path("handlers/clientplatform_entry.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function_names = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("clientplatform_entry_start", function_names)
        self.assertIn("open_business_workspace", function_names)
        self.assertIn("open_customer_workspace", function_names)
        self.assertIn("clientplatform_entry_error", function_names)

    def test_start_resolves_both_roles_before_selecting_a_portal(self) -> None:
        source = Path("handlers/clientplatform_entry.py").read_text(encoding="utf-8")
        self.assertIn("asyncio.gather", source)
        self.assertIn("list_accessible_businesses", source)
        self.assertIn("list_customer_businesses", source)
        self.assertIn("if accesses and links:", source)
        self.assertIn("Мои бизнесы", source)
        self.assertIn("Мои специалисты и программы", source)

    def test_role_callbacks_recheck_live_access(self) -> None:
        source = Path("handlers/clientplatform_entry.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
        }
        businesses = ast.get_source_segment(source, functions["open_business_workspace"])
        customers = ast.get_source_segment(source, functions["open_customer_workspace"])
        self.assertIsNotNone(businesses)
        self.assertIsNotNone(customers)
        self.assertIn("list_accessible_businesses", businesses or "")
        self.assertIn("list_customer_businesses", customers or "")
        self.assertIn("if not accesses:", businesses or "")
        self.assertIn("if not links:", customers or "")

    def test_entry_module_owns_idempotent_router_composition(self) -> None:
        entry_source = Path("handlers/clientplatform_entry.py").read_text(encoding="utf-8")
        package_source = Path("handlers/__init__.py").read_text(encoding="utf-8")
        package_tree = ast.parse(package_source)

        self.assertIn(
            'control = importlib.import_module(".clientplatform_control", __package__)',
            entry_source,
        )
        self.assertIn("router.include_router(original_router)", entry_source)
        self.assertIn("control.router = router", entry_source)
        self.assertIn("_dual_role_entry_composed", entry_source)
        self.assertIn('importlib.import_module(".clientplatform_entry", __name__)', package_source)
        self.assertTrue(
            any(
                isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
                for node in package_tree.body
            )
        )


if __name__ == "__main__":
    unittest.main()
