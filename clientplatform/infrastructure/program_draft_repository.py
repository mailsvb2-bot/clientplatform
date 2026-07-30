from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.programs import (
    ContentKind,
    Lesson,
    LessonStatus,
    Program,
    ProgramInvariantViolation,
    ProgramNotFound,
    ProgramRecord,
    ProgramStatus,
    normalize_content_kind,
    normalize_content_ref,
    normalize_lesson_title,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.program_repository import ProgramRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_value(row: Any, name: str, index: int = 0) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    try:
        return row[name]
    except (KeyError, TypeError, IndexError):
        return row[index]


class ProgramDraftRepository:
    """Owner-only lifecycle and lesson editing operations for program drafts."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)
        self._programs = ProgramRepository(conn)

    @staticmethod
    def _active_record(record: ProgramRecord) -> ProgramRecord:
        return ProgramRecord(
            program=record.program,
            lessons=tuple(
                lesson
                for lesson in record.lessons
                if lesson.status == LessonStatus.ACTIVE
            ),
        )

    def _resolve_manager(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_programs()
        return current

    def _lock_draft_program(
        self,
        *,
        current: TenantContext,
        program_id: str,
    ) -> ProgramRecord:
        normalized_program_id = normalize_uuid(
            program_id,
            field_name="program_id",
        )
        cursor = self._conn.execute(
            """
            UPDATE programs
            SET updated_at=updated_at
            WHERE id=? AND business_id=? AND status!='archived'
            """,
            (normalized_program_id, current.business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ProgramNotFound(
                "program was not found in the active business"
            )
        record = self._programs.get_program(
            actor=current,
            program_id=normalized_program_id,
        )
        if record.program.status != ProgramStatus.DRAFT:
            raise ProgramInvariantViolation("only a draft program can be edited")
        return self._active_record(record)

    def _lesson_program_id(
        self,
        *,
        current: TenantContext,
        lesson_id: str,
    ) -> tuple[str, str]:
        normalized_lesson_id = normalize_uuid(
            lesson_id,
            field_name="lesson_id",
        )
        row = self._conn.execute(
            """
            SELECT program_id
            FROM lessons
            WHERE id=? AND business_id=? AND status='active'
            """,
            (normalized_lesson_id, current.business_id),
        ).fetchone()
        if row is None:
            raise ProgramNotFound(
                "lesson was not found in the active business"
            )
        return normalized_lesson_id, str(_row_value(row, "program_id"))

    @staticmethod
    def _lesson_from_record(
        record: ProgramRecord,
        *,
        lesson_id: str,
    ) -> Lesson:
        for lesson in record.lessons:
            if lesson.id == lesson_id:
                return lesson
        raise ProgramNotFound("lesson was not found in the draft program")

    def _locked_lesson(
        self,
        *,
        actor: TenantContext,
        lesson_id: str,
    ) -> tuple[TenantContext, ProgramRecord, Lesson]:
        current = self._resolve_manager(actor)
        normalized_lesson_id, program_id = self._lesson_program_id(
            current=current,
            lesson_id=lesson_id,
        )
        record = self._lock_draft_program(
            current=current,
            program_id=program_id,
        )
        return (
            current,
            record,
            self._lesson_from_record(
                record,
                lesson_id=normalized_lesson_id,
            ),
        )

    def _touch_program(
        self,
        *,
        current: TenantContext,
        program_id: str,
        timestamp: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE programs
            SET updated_at=?
            WHERE id=? AND business_id=? AND status='draft'
            """,
            (timestamp, program_id, current.business_id),
        )

    def _maximum_position(
        self,
        *,
        current: TenantContext,
        program_id: str,
    ) -> int:
        row = self._conn.execute(
            """
            SELECT COALESCE(MAX(position), 0) AS maximum
            FROM lessons
            WHERE business_id=? AND program_id=?
            """,
            (current.business_id, program_id),
        ).fetchone()
        return int(_row_value(row, "maximum") or 0)

    def list_drafts(self, *, actor: TenantContext) -> list[Program]:
        current = self._resolve_manager(actor)
        return [
            program
            for program in self._programs.list_programs(actor=current)
            if program.status == ProgramStatus.DRAFT
        ]

    def get_draft(
        self,
        *,
        actor: TenantContext,
        program_id: str,
    ) -> ProgramRecord:
        current = self._resolve_manager(actor)
        record = self._programs.get_program(
            actor=current,
            program_id=program_id,
        )
        if record.program.status != ProgramStatus.DRAFT:
            raise ProgramInvariantViolation("only a draft program can be edited")
        return self._active_record(record)

    def get_lesson_draft(
        self,
        *,
        actor: TenantContext,
        lesson_id: str,
    ) -> tuple[ProgramRecord, Lesson]:
        current = self._resolve_manager(actor)
        normalized_lesson_id, program_id = self._lesson_program_id(
            current=current,
            lesson_id=lesson_id,
        )
        record = self.get_draft(actor=current, program_id=program_id)
        return (
            record,
            self._lesson_from_record(
                record,
                lesson_id=normalized_lesson_id,
            ),
        )

    def update_lesson_title(
        self,
        *,
        actor: TenantContext,
        lesson_id: str,
        title: str,
        now: str | None = None,
    ) -> tuple[ProgramRecord, Lesson]:
        normalized_title = normalize_lesson_title(title)
        timestamp = str(now or _utc_now())
        current, record, lesson = self._locked_lesson(
            actor=actor,
            lesson_id=lesson_id,
        )
        cursor = self._conn.execute(
            """
            UPDATE lessons
            SET title=?, updated_at=?
            WHERE id=? AND business_id=? AND program_id=? AND status='active'
            """,
            (
                normalized_title,
                timestamp,
                lesson.id,
                current.business_id,
                record.program.id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ProgramNotFound("lesson was not found in the draft program")
        self._touch_program(
            current=current,
            program_id=record.program.id,
            timestamp=timestamp,
        )
        return self.get_lesson_draft(actor=current, lesson_id=lesson.id)

    def replace_lesson_content(
        self,
        *,
        actor: TenantContext,
        lesson_id: str,
        content_kind: ContentKind | str,
        content_ref: str,
        now: str | None = None,
    ) -> tuple[ProgramRecord, Lesson]:
        normalized_kind = normalize_content_kind(content_kind)
        normalized_ref = normalize_content_ref(content_ref)
        timestamp = str(now or _utc_now())
        current, record, lesson = self._locked_lesson(
            actor=actor,
            lesson_id=lesson_id,
        )
        cursor = self._conn.execute(
            """
            UPDATE lessons
            SET content_kind=?, content_ref=?, updated_at=?
            WHERE id=? AND business_id=? AND program_id=? AND status='active'
            """,
            (
                normalized_kind.value,
                normalized_ref,
                timestamp,
                lesson.id,
                current.business_id,
                record.program.id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ProgramNotFound("lesson was not found in the draft program")
        self._touch_program(
            current=current,
            program_id=record.program.id,
            timestamp=timestamp,
        )
        return self.get_lesson_draft(actor=current, lesson_id=lesson.id)

    def move_lesson(
        self,
        *,
        actor: TenantContext,
        lesson_id: str,
        direction: str,
        now: str | None = None,
    ) -> ProgramRecord:
        normalized_direction = str(direction or "").strip().lower()
        if normalized_direction not in {"up", "down"}:
            raise ValueError("direction must be 'up' or 'down'")
        timestamp = str(now or _utc_now())
        current, record, lesson = self._locked_lesson(
            actor=actor,
            lesson_id=lesson_id,
        )
        lessons = list(record.lessons)
        index = next(
            position
            for position, item in enumerate(lessons)
            if item.id == lesson.id
        )
        neighbor_index = index - 1 if normalized_direction == "up" else index + 1
        if neighbor_index < 0 or neighbor_index >= len(lessons):
            raise ProgramInvariantViolation(
                f"lesson is already at the {normalized_direction} boundary"
            )
        neighbor = lessons[neighbor_index]
        scratch_position = self._maximum_position(
            current=current,
            program_id=record.program.id,
        ) + 1
        self._conn.execute(
            """
            UPDATE lessons SET position=?, updated_at=?
            WHERE id=? AND business_id=? AND program_id=? AND status='active'
            """,
            (
                scratch_position,
                timestamp,
                lesson.id,
                current.business_id,
                record.program.id,
            ),
        )
        self._conn.execute(
            """
            UPDATE lessons SET position=?, updated_at=?
            WHERE id=? AND business_id=? AND program_id=? AND status='active'
            """,
            (
                lesson.position,
                timestamp,
                neighbor.id,
                current.business_id,
                record.program.id,
            ),
        )
        self._conn.execute(
            """
            UPDATE lessons SET position=?, updated_at=?
            WHERE id=? AND business_id=? AND program_id=? AND status='active'
            """,
            (
                neighbor.position,
                timestamp,
                lesson.id,
                current.business_id,
                record.program.id,
            ),
        )
        self._touch_program(
            current=current,
            program_id=record.program.id,
            timestamp=timestamp,
        )
        return self.get_draft(actor=current, program_id=record.program.id)

    def archive_lesson(
        self,
        *,
        actor: TenantContext,
        lesson_id: str,
        now: str | None = None,
    ) -> ProgramRecord:
        timestamp = str(now or _utc_now())
        current, record, lesson = self._locked_lesson(
            actor=actor,
            lesson_id=lesson_id,
        )
        maximum_position = self._maximum_position(
            current=current,
            program_id=record.program.id,
        )
        scratch_position = maximum_position + 1
        cursor = self._conn.execute(
            """
            UPDATE lessons
            SET position=?, status='archived', archived_at=?, updated_at=?
            WHERE id=? AND business_id=? AND program_id=? AND status='active'
            """,
            (
                scratch_position,
                timestamp,
                timestamp,
                lesson.id,
                current.business_id,
                record.program.id,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ProgramNotFound("lesson was not found in the draft program")

        offset = scratch_position
        self._conn.execute(
            """
            UPDATE lessons
            SET position=position+?, updated_at=?
            WHERE business_id=? AND program_id=?
              AND status='active' AND position>?
            """,
            (
                offset,
                timestamp,
                current.business_id,
                record.program.id,
                lesson.position,
            ),
        )
        self._conn.execute(
            """
            UPDATE lessons
            SET position=position-?-1, updated_at=?
            WHERE business_id=? AND program_id=?
              AND status='active' AND position>?
            """,
            (
                offset,
                timestamp,
                current.business_id,
                record.program.id,
                lesson.position + offset,
            ),
        )
        self._touch_program(
            current=current,
            program_id=record.program.id,
            timestamp=timestamp,
        )
        return self.get_draft(actor=current, program_id=record.program.id)

    def archive_draft(
        self,
        *,
        actor: TenantContext,
        program_id: str,
        now: str | None = None,
    ) -> Program:
        current = self._resolve_manager(actor)
        record = self._lock_draft_program(
            current=current,
            program_id=program_id,
        )
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            UPDATE lessons
            SET status='archived', archived_at=?, updated_at=?
            WHERE business_id=? AND program_id=? AND status='active'
            """,
            (
                timestamp,
                timestamp,
                current.business_id,
                record.program.id,
            ),
        )
        self._conn.execute(
            """
            UPDATE programs
            SET status='archived', archived_at=?, updated_at=?
            WHERE id=? AND business_id=? AND status='draft'
            """,
            (
                timestamp,
                timestamp,
                record.program.id,
                current.business_id,
            ),
        )
        return self._programs.get_program(
            actor=current,
            program_id=record.program.id,
        ).program


__all__ = ["ProgramDraftRepository"]
