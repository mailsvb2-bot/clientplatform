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

    def test_builder_persists_title_lessons_publish_and_archive(self) -> None:
        source = Path("handlers/clientplatform_program_builder.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        title = ast.get_source_segment(source, functions["capture_program_title"])
        capture = ast.get_source_segment(source, functions["capture_lesson_content"])
        publish = ast.get_source_segment(source, functions["publish_draft"])
        archive = ast.get_source_segment(source, functions["archive_draft"])

        self.assertIn("create_program", title or "")
        self.assertIn("add_program_lesson", capture or "")
        self.assertIn("normalize_content_ref", capture or "")
        self.assertIn("2048", capture or "")
        self.assertIn("publish_program", publish or "")
        self.assertIn("archive_program_draft", archive or "")
        self.assertIn("state.clear", archive or "")

    def test_callbacks_are_self_contained_and_fit_telegram_limit(self) -> None:
        source = Path("handlers/clientplatform_program_builder.py").read_text(
            encoding="utf-8"
        )
        callbacks = (
            "cp:dopen:",
            "cp:dadd:",
            "cp:dpub:",
            "cp:darc:",
        )
        longest = "cp:dopen:" + ("x" * 22) + ":" + ("y" * 22)
        self.assertLessEqual(len(longest.encode("utf-8")), 64)
        for callback in callbacks:
            self.assertIn(callback.split(":", 2)[1], source)
        self.assertIn("_callback_program_ids", source)
        self.assertIn("_session_ids", source)
        self.assertIn("await control._actor", source)

    def test_delivery_filters_out_draft_programs(self) -> None:
        source = Path("handlers/clientplatform_program_builder.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        delivery = ast.get_source_segment(
            source,
            functions["choose_active_program_for_delivery"],
        )
        self.assertIsNotNone(delivery)
        self.assertIn("ProgramStatus.ACTIVE", delivery or "")
        self.assertNotIn("ProgramStatus.DRAFT", delivery or "")

    def test_draft_repository_is_owner_managed_and_row_locked(self) -> None:
        source = Path(
            "clientplatform/infrastructure/program_draft_repository.py"
        ).read_text(encoding="utf-8")
        self.assertIn("assert_can_manage_programs", source)
        self.assertIn("SET updated_at=updated_at", source)
        self.assertIn("status!='archived'", source)
        self.assertIn("only a draft program can be edited", source)
        self.assertIn("UPDATE lessons", source)
        self.assertIn("UPDATE programs", source)

    def test_atomic_batch_use_case_remains_available(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
