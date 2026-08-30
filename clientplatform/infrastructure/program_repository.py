from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

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
    normalize_position,
    normalize_program_title,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure import TenancyRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _program_from_row(row: Any) -> Program:
    published_at = _value(row, "published_at", 7)
    archived_at = _value(row, "archived_at", 8)
    return Program(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        title=str(_value(row, "title", 2)),
        status=ProgramStatus(str(_value(row, "status", 3))),
        created_by_member_id=str(_value(row, "created_by_member_id", 4)),
        created_at=str(_value(row, "created_at", 5)),
        updated_at=str(_value(row, "updated_at", 6)),
        published_at=None if published_at is None else str(published_at),
        archived_at=None if archived_at is None else str(archived_at),
    )


def _idempotent_uuid(*, business_id: str, kind: str, key: str | None) -> str:
    if key is None:
        return str(uuid4())
    normalized = str(key).strip()
    if not normalized or len(normalized) > 500:
        raise ValueError("idempotency_key must be 1..500 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError("idempotency_key contains control characters")
    return str(
        uuid5(
            NAMESPACE_URL,
            f"clientplatform:{kind}:{business_id}:{normalized}",
        )
    )


def _lesson_from_row(row: Any) -> Lesson:
    archived_at = _value(row, "archived_at", 10)
    return Lesson(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        program_id=str(_value(row, "program_id", 2)),
        position=int(_value(row, "position", 3)),
        title=str(_value(row, "title", 4)),
        content_kind=ContentKind(str(_value(row, "content_kind", 5))),
        content_ref=str(_value(row, "content_ref", 6)),
        status=LessonStatus(str(_value(row, "status", 7))),
        created_at=str(_value(row, "created_at", 8)),
        updated_at=str(_value(row, "updated_at", 9)),
        archived_at=None if archived_at is None else str(archived_at),
    )


class ProgramRepository:
    """Program definitions isolated by a server-resolved TenantContext."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _resolve_actor(
        self,
        actor: TenantContext,
        *,
        manage: bool,
    ) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        if manage:
            current.assert_can_manage_programs()
        else:
            current.assert_can_view_programs()
        return current

    def create_program(
        self,
        *,
        actor: TenantContext,
        title: str,
        idempotency_key: str | None = None,
        now: str | None = None,
    ) -> Program:
        current = self._resolve_actor(actor, manage=True)
        normalized_title = normalize_program_title(title)
        program_id = _idempotent_uuid(
            business_id=current.business_id,
            kind="program",
            key=idempotency_key,
        )
        if idempotency_key is not None:
            existing = self._conn.execute(
                """
                SELECT id, business_id, title, status, created_by_member_id,
                       created_at, updated_at, published_at, archived_at
                FROM programs
                WHERE id=? AND business_id=?
                LIMIT 1
                """,
                (program_id, current.business_id),
            ).fetchone()
            if existing is not None:
                program = _program_from_row(existing)
                if program.title != normalized_title:
                    raise ProgramInvariantViolation(
                        "program idempotency key belongs to different work"
                    )
                return program
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO programs(
                id, business_id, title, status, created_by_member_id,
                created_at, updated_at, published_at, archived_at
            ) VALUES(?, ?, ?, 'draft', ?, ?, ?, NULL, NULL)
            """,
            (
                program_id,
                current.business_id,
                normalized_title,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        return self.get_program(
            actor=current,
            program_id=program_id,
        ).program

    def add_lesson(
        self,
        *,
        actor: TenantContext,
        program_id: str,
        title: str,
        content_kind: ContentKind | str,
        content_ref: str,
        position: int | None = None,
        idempotency_key: str | None = None,
        now: str | None = None,
    ) -> Lesson:
        current = self._resolve_actor(actor, manage=True)
        normalized_program_id = normalize_uuid(
            program_id,
            field_name="program_id",
        )
        normalized_title = normalize_lesson_title(title)
        normalized_kind = normalize_content_kind(content_kind)
        normalized_ref = normalize_content_ref(content_ref)
        lesson_id = _idempotent_uuid(
            business_id=current.business_id,
            kind="lesson",
            key=idempotency_key,
        )
        if idempotency_key is not None:
            existing = self._conn.execute(
                """
                SELECT id, business_id, program_id, position, title, content_kind,
                       content_ref, status, created_at, updated_at, archived_at
                FROM lessons
                WHERE id=? AND business_id=?
                LIMIT 1
                """,
                (lesson_id, current.business_id),
            ).fetchone()
            if existing is not None:
                lesson = _lesson_from_row(existing)
                expected_position = None if position is None else normalize_position(position)
                if (
                    lesson.program_id != normalized_program_id
                    or lesson.title != normalized_title
                    or lesson.content_kind != normalized_kind
                    or lesson.content_ref != normalized_ref
                    or (expected_position is not None and lesson.position != expected_position)
                ):
                    raise ProgramInvariantViolation(
                        "lesson idempotency key belongs to different work"
                    )
                return lesson
        timestamp = str(now or _utc_now())

        self._lock_program(
            business_id=current.business_id,
            program_id=normalized_program_id,
        )
        program = self._get_program_row(
            business_id=current.business_id,
            program_id=normalized_program_id,
        )
        if program.status != ProgramStatus.DRAFT:
            raise ProgramInvariantViolation(
                "lessons can only be added while a program is draft"
            )

        if position is None:
            row = self._conn.execute(
                """
                SELECT COALESCE(MAX(position), 0) AS max_position
                FROM lessons
                WHERE business_id=? AND program_id=? AND status='active'
                """,
                (current.business_id, normalized_program_id),
            ).fetchone()
            lesson_position = int(_value(row, "max_position", 0)) + 1
        else:
            lesson_position = normalize_position(position)

        try:
            self._conn.execute(
                """
                INSERT INTO lessons(
                    id, business_id, program_id, position, title,
                    content_kind, content_ref, status, created_at,
                    updated_at, archived_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)
                """,
                (
                    lesson_id,
                    current.business_id,
                    normalized_program_id,
                    lesson_position,
                    normalized_title,
                    normalized_kind.value,
                    normalized_ref,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ProgramInvariantViolation(
                "lesson position must be unique inside the program"
            ) from exc
        return self._get_lesson(
            business_id=current.business_id,
            lesson_id=lesson_id,
        )

    def publish_program(
        self,
        *,
        actor: TenantContext,
        program_id: str,
        now: str | None = None,
    ) -> Program:
        current = self._resolve_actor(actor, manage=True)
        normalized_program_id = normalize_uuid(
            program_id,
            field_name="program_id",
        )
        self._lock_program(
            business_id=current.business_id,
            program_id=normalized_program_id,
        )
        program = self._get_program_row(
            business_id=current.business_id,
            program_id=normalized_program_id,
        )
        if program.status == ProgramStatus.ACTIVE:
            return program
        if program.status != ProgramStatus.DRAFT:
            raise ProgramInvariantViolation(
                "only a draft program can be published"
            )
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM lessons
            WHERE business_id=? AND program_id=? AND status='active'
            """,
            (current.business_id, normalized_program_id),
        ).fetchone()
        if int(_value(row, "c", 0)) <= 0:
            raise ProgramInvariantViolation(
                "a program must contain at least one active lesson"
            )
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            UPDATE programs
            SET status='active', published_at=?, updated_at=?
            WHERE id=? AND business_id=? AND status='draft'
            """,
            (
                timestamp,
                timestamp,
                normalized_program_id,
                current.business_id,
            ),
        )
        return self._get_program_row(
            business_id=current.business_id,
            program_id=normalized_program_id,
        )

    def get_program(
        self,
        *,
        actor: TenantContext,
        program_id: str,
    ) -> ProgramRecord:
        current = self._resolve_actor(actor, manage=False)
        normalized_program_id = normalize_uuid(
            program_id,
            field_name="program_id",
        )
        program = self._get_program_row(
            business_id=current.business_id,
            program_id=normalized_program_id,
        )
        rows = self._conn.execute(
            """
            SELECT id, business_id, program_id, position, title,
                   content_kind, content_ref, status, created_at,
                   updated_at, archived_at
            FROM lessons
            WHERE business_id=? AND program_id=?
            ORDER BY position, id
            """,
            (current.business_id, normalized_program_id),
        ).fetchall()
        return ProgramRecord(
            program=program,
            lessons=tuple(_lesson_from_row(row) for row in rows),
        )

    def list_programs(
        self,
        *,
        actor: TenantContext,
        include_archived: bool = False,
    ) -> list[Program]:
        current = self._resolve_actor(actor, manage=False)
        if include_archived:
            rows = self._conn.execute(
                """
                SELECT id, business_id, title, status, created_by_member_id,
                       created_at, updated_at, published_at, archived_at
                FROM programs
                WHERE business_id=?
                ORDER BY created_at, id
                """,
                (current.business_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, business_id, title, status, created_by_member_id,
                       created_at, updated_at, published_at, archived_at
                FROM programs
                WHERE business_id=? AND status!='archived'
                ORDER BY created_at, id
                """,
                (current.business_id,),
            ).fetchall()
        return [_program_from_row(row) for row in rows]

    def _lock_program(
        self,
        *,
        business_id: str,
        program_id: str,
    ) -> None:
        cursor = self._conn.execute(
            """
            UPDATE programs
            SET updated_at=updated_at
            WHERE id=? AND business_id=? AND status!='archived'
            """,
            (program_id, business_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ProgramNotFound(
                "program was not found in the active business"
            )

    def _get_program_row(
        self,
        *,
        business_id: str,
        program_id: str,
    ) -> Program:
        row = self._conn.execute(
            """
            SELECT id, business_id, title, status, created_by_member_id,
                   created_at, updated_at, published_at, archived_at
            FROM programs
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (program_id, business_id),
        ).fetchone()
        if row is None:
            raise ProgramNotFound(
                "program was not found in the active business"
            )
        return _program_from_row(row)

    def _get_lesson(
        self,
        *,
        business_id: str,
        lesson_id: str,
    ) -> Lesson:
        normalized_lesson_id = normalize_uuid(
            lesson_id,
            field_name="lesson_id",
        )
        row = self._conn.execute(
            """
            SELECT id, business_id, program_id, position, title,
                   content_kind, content_ref, status, created_at,
                   updated_at, archived_at
            FROM lessons
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_lesson_id, business_id),
        ).fetchone()
        if row is None:
            raise ProgramNotFound(
                "lesson was not found in the active business"
            )
        return _lesson_from_row(row)
