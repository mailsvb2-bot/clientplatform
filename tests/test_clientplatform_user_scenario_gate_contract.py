from __future__ import annotations

import ast
import unittest
from pathlib import Path


class ClientPlatformUserScenarioGateContractTests(unittest.TestCase):
    def test_canonical_vertical_is_a_named_first_class_scenario_group(self) -> None:
        path = Path("scripts/all_user_scenario_gate.py")
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        assignments: dict[str, ast.AST] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assignments[target.id] = node.value

        clientplatform_tests = assignments.get("CLIENTPLATFORM_SCENARIO_TESTS")
        steps = assignments.get("STEPS")
        self.assertIsInstance(clientplatform_tests, ast.Tuple)
        self.assertIsInstance(steps, ast.Tuple)
        assert isinstance(clientplatform_tests, ast.Tuple)
        assert isinstance(steps, ast.Tuple)

        files = {
            item.value
            for item in clientplatform_tests.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        self.assertIn("tests/test_clientplatform_first_vertical_e2e.py", files)
        self.assertIn("tests/test_handlers_clientplatform_managed_bot_entry.py", files)
        self.assertIn("tests/test_clientplatform_program_media_ingest.py", files)
        self.assertIn("tests/test_clientplatform_program_progress_portal.py", files)
        self.assertGreaterEqual(len(files), 5)

        step_names: list[str] = []
        for item in steps.elts:
            if not isinstance(item, ast.Call) or not item.args:
                continue
            first = item.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                step_names.append(first.value)
        self.assertTrue(step_names)
        self.assertEqual(step_names[0], "ClientPlatform canonical first vertical")
        self.assertIn("*CLIENTPLATFORM_SCENARIO_TESTS", source)
        self.assertIn(
            "len(CLIENTPLATFORM_SCENARIO_TESTS) + len(SCENARIO_TESTS)",
            source,
        )

    def test_workflow_executes_the_canonical_gate(self) -> None:
        workflow = Path(".github/workflows/user-scenario-matrix.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python scripts/all_user_scenario_gate.py", workflow)
        self.assertIn("all-user-scenarios.log", workflow)


if __name__ == "__main__":
    unittest.main()
