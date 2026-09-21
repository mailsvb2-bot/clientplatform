from __future__ import annotations

from clientplatform.domain.activity_directions import (
    ActivityDirection,
    ActivityDirectionBinding,
    DirectionSubjectKind,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.activity_direction_repository import (
    ActivityDirectionRepository,
)
from services.db import get_db, get_db_ro


def create_activity_direction(
    *,
    actor: TenantContext,
    title: str,
    description: str,
) -> ActivityDirection:
    with get_db() as conn:
        return ActivityDirectionRepository(conn).create(
            actor=actor,
            title=title,
            description=description,
        )


def update_activity_direction(
    *,
    actor: TenantContext,
    direction_id: str,
    title: str,
    description: str,
) -> ActivityDirection:
    with get_db() as conn:
        return ActivityDirectionRepository(conn).update(
            actor=actor,
            direction_id=direction_id,
            title=title,
            description=description,
        )


def archive_activity_direction(
    *,
    actor: TenantContext,
    direction_id: str,
) -> ActivityDirection:
    with get_db() as conn:
        return ActivityDirectionRepository(conn).archive(
            actor=actor,
            direction_id=direction_id,
        )


def restore_activity_direction(
    *,
    actor: TenantContext,
    direction_id: str,
) -> ActivityDirection:
    with get_db() as conn:
        return ActivityDirectionRepository(conn).restore(
            actor=actor,
            direction_id=direction_id,
        )


def get_activity_direction(
    *,
    actor: TenantContext,
    direction_id: str,
) -> ActivityDirection:
    with get_db_ro() as conn:
        return ActivityDirectionRepository(conn).get(
            actor=actor,
            direction_id=direction_id,
        )


def list_activity_directions(
    *,
    actor: TenantContext,
    include_archived: bool = False,
) -> list[ActivityDirection]:
    with get_db_ro() as conn:
        return ActivityDirectionRepository(conn).list(
            actor=actor,
            include_archived=include_archived,
        )


def bind_activity_direction_subject(
    *,
    actor: TenantContext,
    direction_id: str,
    subject_kind: DirectionSubjectKind | str,
    subject_id: str,
) -> ActivityDirectionBinding:
    with get_db() as conn:
        return ActivityDirectionRepository(conn).bind_subject(
            actor=actor,
            direction_id=direction_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
        )


def unbind_activity_direction_subject(
    *,
    actor: TenantContext,
    subject_kind: DirectionSubjectKind | str,
    subject_id: str,
) -> bool:
    with get_db() as conn:
        return ActivityDirectionRepository(conn).unbind_subject(
            actor=actor,
            subject_kind=subject_kind,
            subject_id=subject_id,
        )


def list_activity_direction_bindings(
    *,
    actor: TenantContext,
    direction_id: str,
    subject_kind: DirectionSubjectKind | str | None = None,
) -> list[ActivityDirectionBinding]:
    with get_db_ro() as conn:
        return ActivityDirectionRepository(conn).list_bindings(
            actor=actor,
            direction_id=direction_id,
            subject_kind=subject_kind,
        )


__all__ = [
    "archive_activity_direction",
    "bind_activity_direction_subject",
    "create_activity_direction",
    "get_activity_direction",
    "list_activity_direction_bindings",
    "list_activity_directions",
    "restore_activity_direction",
    "unbind_activity_direction_subject",
    "update_activity_direction",
]
