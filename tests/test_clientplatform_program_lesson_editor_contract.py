from __future__ import annotations

import ast
import unittest
from pathlib import Path


class ClientPlatformProgramLessonEditorContractTests(unittest.TestCase):
    def test_entry_composes_editor_before_builder_and_legacy_router(self) -> None:
        source = Path("handlers/clientplatform_entry.py").read_text(encoding="utf-8")
        editor_include = "router.include_router(lesson_editor.router)"
        builder_include = "router.include_router(program_builder.router)"
        legacy_include = "router.include_router(original_router)"

        self.assertIn("clientplatform_program_lesson_editor_composition", source)
        self.assertIn(editor_include, source)
        self.assertIn(builder_include, source)
        self.assertIn(legacy_include, source)
        self.assertLess(source.index(editor_include), source.index(builder_include))
        self.assertLess(source.index(builder_include), source.index(legacy_include))
        self.assertIn("_program_lesson_editor_composed", source)

    def test_composition_adds_editor_without_rewriting_builder(self) -> None:
        source = Path(
            "handlers/clientplatform_program_lesson_editor_composition.py"
        ).read_text(encoding="utf-8")
        self.assertIn("Заменить или удалить материал", source)
        self.assertNotIn("Редактировать уроки", source)
        self.assertIn("builder._draft_keyboard =", source)
        self.assertIn("_draft_lesson_editor_composed", source)
        self.assertIn("router = editor.router", source)

    def test_callbacks_fit_telegram_limit_with_two_uuid_tokens(self) -> None:
        source = Path("handlers/clientplatform_program_lesson_editor.py").read_text(
            encoding="utf-8"
        )
        longest_lesson = "cp:dlcancel:" + ("b" * 22) + ":" + ("l" * 22)
        longest_program = "cp:dless:" + ("b" * 22) + ":" + ("p" * 22) + ":99"
        self.assertLessEqual(len(longest_lesson.encode("utf-8")), 64)
        self.assertLessEqual(len(longest_program.encode("utf-8")), 64)
        for action in (
            "dless",
            "dled",
            "dlname",
            "dlmat",
            "dlup",
            "dldown",
            "dlask",
            "dldel",
            "dlcancel",
        ):
            self.assertIn(action, source)
        self.assertNotIn("program_id, lesson_id", source)

    def test_editor_hides_non_text_content_references(self) -> None:
        source = Path("handlers/clientplatform_program_lesson_editor.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        detail = ast.get_source_segment(source, functions["_lesson_detail_text"])
        self.assertIsNotNone(detail)
        self.assertIn("ContentKind.TEXT", detail or "")
        self.assertIn("lesson.content_ref[:500]", detail or "")
        self.assertNotIn("file_id", detail or "")

    def test_repository_serializes_edits_and_uses_safe_scratch_positions(self) -> None:
        source = Path(
            "clientplatform/infrastructure/program_draft_repository.py"
        ).read_text(encoding="utf-8")
        self.assertIn("SET updated_at=updated_at", source)
        self.assertIn("only a draft program can be edited", source)
        self.assertIn("scratch_position =", source)
        self.assertIn("MAX(position)", source)
        self.assertIn("position=position+?", source)
        self.assertIn("position=position-?-1", source)
        self.assertIn("status='archived'", source)
        self.assertIn("assert_can_manage_programs", source)

    def test_application_api_keeps_read_and_write_boundaries_explicit(self) -> None:
        source = Path("clientplatform/application/programs.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        expected = {
            "get_program_draft_lesson": "get_db_ro",
            "update_program_draft_lesson_title": "get_db",
            "replace_program_draft_lesson_content": "get_db",
            "move_program_draft_lesson": "get_db",
            "archive_program_draft_lesson": "get_db",
        }
        for name, boundary in expected.items():
            self.assertIn(name, functions)
            function = ast.get_source_segment(source, functions[name]) or ""
            self.assertIn(f"with {boundary}() as conn:", function)
            self.assertIn("ProgramDraftRepository(conn)", function)


if __name__ == "__main__":
    unittest.main()
