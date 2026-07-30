from __future__ import annotations

import ast
import unittest
from pathlib import Path


class ClientPlatformMultiLessonBuilderContractTests(unittest.TestCase):
    def test_entry_composes_builder_before_legacy_control_router(self) -> None:
        source = Path("handlers/clientplatform_entry.py").read_text(encoding="utf-8")
        builder_import = '".clientplatform_program_builder"'
        builder_include = "router.include_router(program_builder.router)"
        legacy_include = "router.include_router(original_router)"

        self.assertIn(builder_import, source)
        self.assertIn(builder_include, source)
        self.assertIn(legacy_include, source)
        self.assertLess(source.index(builder_include), source.index(legacy_include))
        self.assertIn("_multi_lesson_program_builder_composed", source)

    def test_builder_uses_session_state_until_atomic_publish(self) -> None:
        source = Path("handlers/clientplatform_program_builder.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        capture = ast.get_source_segment(source, functions["capture_lesson_content"])
        publish = ast.get_source_segment(source, functions["publish_program"])
        cancel = ast.get_source_segment(source, functions["cancel_program"])

        self.assertIsNotNone(capture)
        self.assertIsNotNone(publish)
        self.assertIsNotNone(cancel)
        self.assertNotIn("create_multi_lesson_program", capture or "")
        self.assertIn("create_multi_lesson_program", publish or "")
        self.assertIn("state.clear", cancel or "")
        self.assertIn("В базе ничего не создавалось", cancel or "")

    def test_builder_has_no_obsolete_persistent_draft_api(self) -> None:
        source = Path("handlers/clientplatform_program_builder.py").read_text(
            encoding="utf-8"
        )
        for obsolete in (
            "append_lesson",
            "get_program_for_owner",
            "list_owner_programs",
            "ProgramSummary",
            "_run_blocking",
            "Сохранить черновик и выйти",
        ):
            self.assertNotIn(obsolete, source)

    def test_callbacks_fit_telegram_limit_and_are_state_scoped(self) -> None:
        source = Path("handlers/clientplatform_program_builder.py").read_text(
            encoding="utf-8"
        )
        callbacks = (
            "cp:pbuild:add",
            "cp:pbuild:publish",
            "cp:pbuild:cancel",
        )
        for callback in callbacks:
            self.assertLessEqual(len(callback.encode("utf-8")), 64)
            self.assertIn(callback, source)
        self.assertIn("_session_data", source)
        self.assertIn("await control._actor", source)

    def test_application_use_case_is_one_database_boundary(self) -> None:
        source = Path("clientplatform/application/program_builder.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        function = ast.get_source_segment(
            source,
            functions["create_multi_lesson_program"],
        )
        self.assertIsNotNone(function)
        self.assertEqual((function or "").count("with get_db() as conn:"), 1)
        self.assertIn("programs.create_program", function or "")
        self.assertIn("programs.add_lesson", function or "")
        self.assertIn("programs.publish_program", function or "")
        self.assertIn("_MAX_LESSONS_PER_PROGRAM = 100", source)


if __name__ == "__main__":
    unittest.main()
