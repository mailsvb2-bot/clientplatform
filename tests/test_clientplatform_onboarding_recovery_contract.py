from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "handlers" / "clientplatform_entry.py"
RECOVERY = ROOT / "handlers" / "clientplatform_onboarding_recovery.py"


class ClientPlatformOnboardingRecoveryContractTests(unittest.TestCase):
    def test_recovery_router_is_composed_before_other_subrouters(self) -> None:
        text = ENTRY.read_text(encoding="utf-8")
        recovery = "router.include_router(onboarding_recovery.router)"
        media = "router.include_router(program_media.router)"
        original = "router.include_router(original_router)"

        self.assertIn(recovery, text)
        self.assertLess(text.index(recovery), text.index(media))
        self.assertLess(text.index(media), text.index(original))
        self.assertIn(
            "control._onboarding_recovery_router_composed = True",
            text,
        )

    def test_recovery_module_has_dependency_light_source_contract(self) -> None:
        tree = ast.parse(RECOVERY.read_text(encoding="utf-8"))
        function_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        class_names = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        }
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }

        self.assertIn("recover_activity_description", function_names)
        self.assertIn("IncompleteActivityDescriptionFilter", class_names)
        self.assertNotIn("os", imported_roots)
        self.assertNotIn("subprocess", imported_roots)


if __name__ == "__main__":
    unittest.main()
