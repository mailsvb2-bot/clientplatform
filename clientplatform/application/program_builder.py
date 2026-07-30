from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from clientplatform.domain.programs import ContentKind, ProgramRecord
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.program_repository import ProgramRepository
from services.db import get_db

_MAX_LESSONS_PER_PROGRAM = 100


@dataclass(frozen=True, slots=True)
class ProgramLessonInput:
    title: str
    content_kind: ContentKind | str
    content_ref: str


def create_multi_lesson_program(
    *,
    actor: TenantContext,
    program_title: str,
    lessons: Sequence[ProgramLessonInput],
) -> ProgramRecord:
    """Create and publish one complete program in a single database boundary."""

    selected_lessons = tuple(lessons)
    if not selected_lessons:
        raise ValueError("program must contain at least one lesson")
    if len(selected_lessons) > _MAX_LESSONS_PER_PROGRAM:
        raise ValueError(
            f"program cannot contain more than {_MAX_LESSONS_PER_PROGRAM} lessons"
        )

    with get_db() as conn:
        programs = ProgramRepository(conn)
        program = programs.create_program(actor=actor, title=program_title)
        for lesson in selected_lessons:
            programs.add_lesson(
                actor=actor,
                program_id=program.id,
                title=lesson.title,
                content_kind=lesson.content_kind,
                content_ref=lesson.content_ref,
            )
        programs.publish_program(actor=actor, program_id=program.id)
        return programs.get_program(actor=actor, program_id=program.id)


__all__ = [
    "ProgramLessonInput",
    "create_multi_lesson_program",
]
