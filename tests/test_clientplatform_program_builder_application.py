from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import program_builder
from clientplatform.domain.programs import ContentKind


class FakeProgramRepository:
    def __init__(self, conn: object) -> None:
        self.conn = conn
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.program = SimpleNamespace(id=str(uuid4()), title="Программа")
        self.lessons: list[SimpleNamespace] = []

    def create_program(self, **kwargs: Any) -> Any:
        self.calls.append(("create", kwargs))
        self.program = SimpleNamespace(id=str(uuid4()), title=kwargs["title"])
        return self.program

    def add_lesson(self, **kwargs: Any) -> Any:
        self.calls.append(("lesson", kwargs))
        lesson = SimpleNamespace(
            title=kwargs["title"],
            content_kind=kwargs["content_kind"],
            content_ref=kwargs["content_ref"],
        )
        self.lessons.append(lesson)
        return lesson

    def publish_program(self, **kwargs: Any) -> Any:
        self.calls.append(("publish", kwargs))
        return self.program

    def get_program(self, **kwargs: Any) -> Any:
        self.calls.append(("get", kwargs))
        return SimpleNamespace(program=self.program, lessons=tuple(self.lessons))


class ClientPlatformProgramBuilderApplicationTests(unittest.TestCase):
    def test_multi_lesson_program_uses_one_database_boundary(self) -> None:
        connection = object()
        repositories: list[FakeProgramRepository] = []
        database_entries = 0

        @contextmanager
        def fake_get_db() -> Iterator[object]:
            nonlocal database_entries
            database_entries += 1
            yield connection

        def fake_repository(conn: object) -> FakeProgramRepository:
            repository = FakeProgramRepository(conn)
            repositories.append(repository)
            return repository

        actor = object()
        with (
            patch.object(program_builder, "get_db", fake_get_db),
            patch.object(program_builder, "ProgramRepository", fake_repository),
        ):
            result = program_builder.create_multi_lesson_program(
                actor=actor,
                program_title="Спокойный сон",
                lessons=(
                    program_builder.ProgramLessonInput(
                        title="Введение",
                        content_kind=ContentKind.TEXT,
                        content_ref="Первый текст",
                    ),
                    program_builder.ProgramLessonInput(
                        title="Практика",
                        content_kind=ContentKind.AUDIO,
                        content_ref="telegram-file-id",
                    ),
                ),
            )

        self.assertEqual(database_entries, 1)
        self.assertEqual(len(repositories), 1)
        repository = repositories[0]
        self.assertIs(repository.conn, connection)
        self.assertEqual(
            [name for name, _kwargs in repository.calls],
            ["create", "lesson", "lesson", "publish", "get"],
        )
        lesson_calls = [
            kwargs for name, kwargs in repository.calls if name == "lesson"
        ]
        self.assertEqual(
            [item["title"] for item in lesson_calls],
            ["Введение", "Практика"],
        )
        self.assertTrue(all(item["actor"] is actor for item in lesson_calls))
        self.assertEqual(result.program.title, "Спокойный сон")
        self.assertEqual(
            [lesson.title for lesson in result.lessons],
            ["Введение", "Практика"],
        )

    def test_multi_lesson_program_rejects_empty_and_oversized_input(self) -> None:
        def forbidden_get_db() -> object:
            raise AssertionError("database must not be opened for invalid input")

        with patch.object(program_builder, "get_db", forbidden_get_db):
            with self.assertRaisesRegex(ValueError, "at least one lesson"):
                program_builder.create_multi_lesson_program(
                    actor=object(),
                    program_title="Пустая программа",
                    lessons=(),
                )

            lesson = program_builder.ProgramLessonInput(
                title="Урок",
                content_kind=ContentKind.TEXT,
                content_ref="Материал",
            )
            with self.assertRaisesRegex(ValueError, "more than 100 lessons"):
                program_builder.create_multi_lesson_program(
                    actor=object(),
                    program_title="Слишком большая программа",
                    lessons=(lesson,) * 101,
                )


if __name__ == "__main__":
    unittest.main()
