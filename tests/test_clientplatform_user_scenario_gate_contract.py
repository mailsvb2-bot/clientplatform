from __future__ import annotations

import ast
import unittest
from pathlib import Path


class ClientPlatformUserScenarioGateContractTests(unittest.TestCase):
    def test_canonical_verticals_are_named_first_class_scenario_groups(self) -> None:
        path = Path("scripts/all_user_scenario_gate.py")
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        assignments: dict[str, ast.AST] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assignments[target.id] = node.value

        expected_groups = {
            "CLIENTPLATFORM_SCENARIO_TESTS",
            "OWNER_RUNTIME_TESTS",
            "OMNICHANNEL_TESTS",
            "COMMERCIAL_OUTCOME_TESTS",
        }
        for name in expected_groups:
            self.assertIsInstance(assignments.get(name), ast.Tuple)

        first = assignments["CLIENTPLATFORM_SCENARIO_TESTS"]
        assert isinstance(first, ast.Tuple)
        files = {
            item.value
            for item in first.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        self.assertIn("tests/test_clientplatform_first_vertical_e2e.py", files)
        self.assertIn("tests/test_handlers_clientplatform_managed_bot_entry.py", files)
        self.assertIn("tests/test_clientplatform_program_media_ingest.py", files)
        self.assertIn("tests/test_clientplatform_program_progress_portal.py", files)

        steps = assignments.get("STEPS")
        self.assertIsInstance(steps, ast.Tuple)
        assert isinstance(steps, ast.Tuple)
        step_names = [
            item.args[0].value
            for item in steps.elts
            if isinstance(item, ast.Call)
            and item.args
            and isinstance(item.args[0], ast.Constant)
            and isinstance(item.args[0].value, str)
        ]
        self.assertEqual(
            step_names,
            [
                "ClientPlatform canonical first vertical",
                "ClientPlatform owner and runtime",
                "ClientPlatform omnichannel parity",
                "ClientPlatform commercial outcomes",
            ],
        )
        self.assertIn("sum(len(step.tests) for step in STEPS)", source)
        self.assertIn("ClientPlatform commercial outcomes", source)

    def test_workflow_executes_the_canonical_gate(self) -> None:
        workflow = Path(".github/workflows/user-scenario-matrix.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python scripts/all_user_scenario_gate.py", workflow)
        self.assertIn("all-user-scenarios.log", workflow)


if __name__ == "__main__":
    unittest.main()
