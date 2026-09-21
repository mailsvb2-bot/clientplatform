from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.activity_directions import (
    ActivityDirection,
    ActivityDirectionBinding,
    ActivityDirectionInvariantViolation,
    ActivityDirectionNotFound,
    ActivityDirectionStatus,
    DirectionSubjectKind,
    normalize_direction_description,
    normalize_direction_subject_kind,
    normalize_direction_title,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _next_revision(previous: str, *, now: str | None = None) -> str:
    candidate = str(now or _utc_now())
    if candidate != previous:
        return candidate
    parsed = datetime.fromisoformat(previous)
    return (parsed + timedelta(microseconds=1)).isoformat(timespec="microseconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _direction_from_row(row: Any) -> ActivityDirection:
    archived_at = _value(row, "archived_at", 8)
    return ActivityDirection(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        title=str(_value(row, "title", 2)),
        description=str(_value(row, "description", 3)),
        status=ActivityDirectionStatus(str(_value(row, "status", 4))),
        created_by_member_id=str(_value(row, "created_by_member_id", 5)),
        created_at=str(_value(row, "created_at", 6)),
        updated_at=str(_value(row, "updated_at", 7)),
        archived_at=None if archived_at is None else str(archived_at),
    )


def _binding_from_row(row: Any) -> ActivityDirectionBinding:
    return ActivityDirectionBinding(
        business_id=str(_value(row, "business_id", 0)),
        direction_id=str(_value(row, "direction_id", 1)),
        subject_kind=DirectionSubjectKind(str(_value(row, "subject_kind", 2))),
        subject_id=str(_value(row, "subject_id", 3)),
        created_by_member_id=str(_value(row, "created_by_member_id", 4)),
        created_at=str(_value(row, "created_at", 5)),
    )


class ActivityDirectionRepository:
    """Canonical organization-level grouping for multiple lines of activity."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _current(self, actor: TenantContext, *, manage: bool) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        if manage:
            current.assert_can_manage_business()
        return current

    @staticmethod
    def _assert_can_bind_subject(
        actor: TenantContext,
        subject_kind: DirectionSubjectKind,
    ) -> None:
        if subject_kind in {
            DirectionSubjectKind.PROGRAM,
            DirectionSubjectKind.OFFERING,
        }:
            actor.assert_can_manage_programs()
            return
        actor.assert_can_manage_business()

    def create(
        self,
        *,
        actor: TenantContext,
        title: str,
        description: str,
        direction_id: str | None = None,
        now: str | None = None,
    ) -> ActivityDirection:
        current = self._current(actor, manage=True)
        normalized_title = normalize_direction_title(title)
        normalized_description = normalize_direction_description(description)
        new_id = normalize_uuid(
            direction_id or str(uuid4()),
            field_name="direction_id",
        )
        timestamp = str(now or _utc_now())
        try:
            self._conn.execute(
                """
                INSERT INTO clientplatform_activity_directions(
                    id,business_id,title,description,status,
                    created_by_member_id,created_at,updated_at,archived_at
                ) VALUES(?,?,?,?, 'active', ?,?,?,NULL)
                """,
                (
                    new_id,
                    current.business_id,
                    normalized_title,
                    normalized_description,
                    current.membership_id,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ActivityDirectionInvariantViolation(
                "activity direction title already exists in this organization"
            ) from exc
        return self.get(actor=current, direction_id=new_id)

    def get(
        self,
        *,
        actor: TenantContext,
        direction_id: str,
    ) -> ActivityDirection:
        current = self._current(actor, manage=False)
        normalized_id = normalize_uuid(direction_id, field_name="direction_id")
        row = self._conn.execute(
            """
            SELECT id,business_id,title,description,status,
                   created_by_member_id,created_at,updated_at,archived_at
            FROM clientplatform_activity_directions
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (normalized_id, current.business_id),
        ).fetchone()
        if row is None:
            raise ActivityDirectionNotFound(
                "activity direction was not found in the active organization"
            )
        return _direction_from_row(row)

    def list(
        self,
        *,
        actor: TenantContext,
        include_archived: bool = False,
    ) -> list[ActivityDirection]:
        current = self._current(actor, manage=False)
        rows = self._conn.execute(
            """
            SELECT id,business_id,title,description,status,
                   created_by_member_id,created_at,updated_at,archived_at
            FROM clientplatform_activity_directions
            WHERE business_id=? AND (? OR status='active')
            ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                     created_at,id
            """,
            (current.business_id, bool(include_archived)),
        ).fetchall()
        return [_direction_from_row(row) for row in rows]

    def update(
        self,
        *,
        actor: TenantContext,
        direction_id: str,
        title: str,
        description: str,
        now: str | None = None,
    ) -> ActivityDirection:
        current = self._current(actor, manage=True)
        direction = self.get(actor=current, direction_id=direction_id)
        if direction.status != ActivityDirectionStatus.ACTIVE:
            raise ActivityDirectionInvariantViolation(
                "archived activity direction must be restored before editing"
            )
        normalized_title = normalize_direction_title(title)
        normalized_description = normalize_direction_description(description)
        if (
            normalized_title == direction.title
            and normalized_description == direction.description
        ):
            return direction
        timestamp = _next_revision(direction.updated_at, now=now)
        try:
            cursor = self._conn.execute(
                """
                UPDATE clientplatform_activity_directions
                SET title=?,description=?,updated_at=?
                WHERE id=? AND business_id=? AND status='active' AND updated_at=?
                """,
                (
                    normalized_title,
                    normalized_description,
                    timestamp,
                    direction.id,
                    current.business_id,
                    direction.updated_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ActivityDirectionInvariantViolation(
                "activity direction title already exists in this organization"
            ) from exc
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ActivityDirectionInvariantViolation(
                "activity direction changed concurrently; refresh and retry"
            )
        return self.get(actor=current, direction_id=direction.id)

    def archive(
        self,
        *,
        actor: TenantContext,
        direction_id: str,
        now: str | None = None,
    ) -> ActivityDirection:
        current = self._current(actor, manage=True)
        direction = self.get(actor=current, direction_id=direction_id)
        if direction.status == ActivityDirectionStatus.ARCHIVED:
            return direction
        timestamp = _next_revision(direction.updated_at, now=now)
        cursor = self._conn.execute(
            """
            UPDATE clientplatform_activity_directions
            SET status='archived',archived_at=?,updated_at=?
            WHERE id=? AND business_id=? AND status='active' AND updated_at=?
            """,
            (
                timestamp,
                timestamp,
                direction.id,
                current.business_id,
                direction.updated_at,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            latest = self.get(actor=current, direction_id=direction.id)
            if latest.status == ActivityDirectionStatus.ARCHIVED:
                return latest
            raise ActivityDirectionInvariantViolation(
                "activity direction changed concurrently; refresh and retry"
            )
        return self.get(actor=current, direction_id=direction.id)

    def restore(
        self,
        *,
        actor: TenantContext,
        direction_id: str,
        now: str | None = None,
    ) -> ActivityDirection:
        current = self._current(actor, manage=True)
        direction = self.get(actor=current, direction_id=direction_id)
        if direction.status == ActivityDirectionStatus.ACTIVE:
            return direction
        timestamp = _next_revision(direction.updated_at, now=now)
        try:
            cursor = self._conn.execute(
                """
                UPDATE clientplatform_activity_directions
                SET status='active',archived_at=NULL,updated_at=?
                WHERE id=? AND business_id=? AND status='archived' AND updated_at=?
                """,
                (
                    timestamp,
                    direction.id,
                    current.business_id,
                    direction.updated_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ActivityDirectionInvariantViolation(
                "activity direction title conflicts with an active direction"
            ) from exc
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            latest = self.get(actor=current, direction_id=direction.id)
            if latest.status == ActivityDirectionStatus.ACTIVE:
                return latest
            raise ActivityDirectionInvariantViolation(
                "activity direction changed concurrently; refresh and retry"
            )
        return self.get(actor=current, direction_id=direction.id)

    def _assert_subject(
        self,
        *,
        business_id: str,
        subject_kind: DirectionSubjectKind,
        subject_id: str,
    ) -> None:
        normalized_id = normalize_uuid(subject_id, field_name="subject_id")
        if subject_kind == DirectionSubjectKind.PROGRAM:
            table = "programs"
        elif subject_kind == DirectionSubjectKind.OFFERING:
            table = "business_offerings"
        else:
            table = "clientplatform_events"
        row = self._conn.execute(
            f"SELECT id FROM {table} WHERE id=? AND business_id=? LIMIT 1",  # nosec B608
            (normalized_id, business_id),
        ).fetchone()
        if row is None:
            raise ActivityDirectionNotFound(
                "activity direction subject was not found in the active organization"
            )

    def bind_subject(
        self,
        *,
        actor: TenantContext,
        direction_id: str,
        subject_kind: DirectionSubjectKind | str,
        subject_id: str,
        now: str | None = None,
    ) -> ActivityDirectionBinding:
        current = self._current(actor, manage=False)
        kind = normalize_direction_subject_kind(subject_kind)
        self._assert_can_bind_subject(current, kind)
        direction = self.get(actor=current, direction_id=direction_id)
        if direction.status != ActivityDirectionStatus.ACTIVE:
            raise ActivityDirectionInvariantViolation(
                "cannot bind work to an archived activity direction"
            )
        normalized_subject_id = normalize_uuid(subject_id, field_name="subject_id")
        self._assert_subject(
            business_id=current.business_id,
            subject_kind=kind,
            subject_id=normalized_subject_id,
        )
        timestamp = str(now or _utc_now())
        self._conn.execute(
            """
            INSERT INTO clientplatform_activity_direction_bindings(
                business_id,direction_id,subject_kind,subject_id,
                created_by_member_id,created_at
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(business_id,subject_kind,subject_id) DO UPDATE SET
                direction_id=excluded.direction_id,
                created_by_member_id=excluded.created_by_member_id,
                created_at=excluded.created_at
            """,
            (
                current.business_id,
                direction.id,
                kind.value,
                normalized_subject_id,
                current.membership_id,
                timestamp,
            ),
        )
        return self.get_binding_for_subject(
            actor=current,
            subject_kind=kind,
            subject_id=normalized_subject_id,
        )

    def unbind_subject(
        self,
        *,
        actor: TenantContext,
        subject_kind: DirectionSubjectKind | str,
        subject_id: str,
    ) -> bool:
        current = self._current(actor, manage=False)
        kind = normalize_direction_subject_kind(subject_kind)
        self._assert_can_bind_subject(current, kind)
        normalized_subject_id = normalize_uuid(subject_id, field_name="subject_id")
        cursor = self._conn.execute(
            """
            DELETE FROM clientplatform_activity_direction_bindings
            WHERE business_id=? AND subject_kind=? AND subject_id=?
            """,
            (current.business_id, kind.value, normalized_subject_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def get_binding_for_subject(
        self,
        *,
        actor: TenantContext,
        subject_kind: DirectionSubjectKind | str,
        subject_id: str,
    ) -> ActivityDirectionBinding:
        current = self._current(actor, manage=False)
        kind = normalize_direction_subject_kind(subject_kind)
        normalized_subject_id = normalize_uuid(subject_id, field_name="subject_id")
        row = self._conn.execute(
            """
            SELECT business_id,direction_id,subject_kind,subject_id,
                   created_by_member_id,created_at
            FROM clientplatform_activity_direction_bindings
            WHERE business_id=? AND subject_kind=? AND subject_id=?
            LIMIT 1
            """,
            (current.business_id, kind.value, normalized_subject_id),
        ).fetchone()
        if row is None:
            raise ActivityDirectionNotFound(
                "activity direction binding was not found in the active organization"
            )
        return _binding_from_row(row)

    def list_bindings(
        self,
        *,
        actor: TenantContext,
        direction_id: str,
        subject_kind: DirectionSubjectKind | str | None = None,
    ) -> list[ActivityDirectionBinding]:
        current = self._current(actor, manage=False)
        direction = self.get(actor=current, direction_id=direction_id)
        if subject_kind is None:
            rows = self._conn.execute(
                """
                SELECT business_id,direction_id,subject_kind,subject_id,
                       created_by_member_id,created_at
                FROM clientplatform_activity_direction_bindings
                WHERE business_id=? AND direction_id=?
                ORDER BY subject_kind,created_at,subject_id
                """,
                (current.business_id, direction.id),
            ).fetchall()
        else:
            kind = normalize_direction_subject_kind(subject_kind)
            rows = self._conn.execute(
                """
                SELECT business_id,direction_id,subject_kind,subject_id,
                       created_by_member_id,created_at
                FROM clientplatform_activity_direction_bindings
                WHERE business_id=? AND direction_id=? AND subject_kind=?
                ORDER BY created_at,subject_id
                """,
                (current.business_id, direction.id, kind.value),
            ).fetchall()
        return [_binding_from_row(row) for row in rows]


__all__ = ["ActivityDirectionRepository"]
