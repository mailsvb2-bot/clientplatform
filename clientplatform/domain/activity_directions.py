from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from clientplatform.domain.tenancy import normalize_uuid


class ActivityDirectionError(RuntimeError):
    """Base error for an organization activity direction."""


class ActivityDirectionNotFound(ActivityDirectionError):
    """Requested direction does not exist in the active organization."""


class ActivityDirectionInvariantViolation(ActivityDirectionError):
    """A direction transition would break an organization invariant."""


class ActivityDirectionStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class DirectionSubjectKind(StrEnum):
    PROGRAM = "program"
    OFFERING = "offering"
    EVENT = "event"


def _normalize_text(value: object, *, field_name: str, maximum: int) -> str:
    raw = str(value or "").replace("\x00", " ")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return normalized


def normalize_direction_title(value: object) -> str:
    return _normalize_text(value, field_name="activity direction title", maximum=160)


def normalize_direction_description(value: object) -> str:
    return _normalize_text(value, field_name="activity direction description", maximum=2000)


def normalize_direction_subject_kind(value: DirectionSubjectKind | str) -> DirectionSubjectKind:
    if isinstance(value, DirectionSubjectKind):
        return value
    try:
        return DirectionSubjectKind(str(value or "").strip().lower())
    except ValueError as exc:
        raise ValueError("unsupported activity direction subject kind") from exc


@dataclass(frozen=True, slots=True)
class ActivityDirection:
    id: str
    business_id: str
    title: str
    description: str
    status: ActivityDirectionStatus
    created_by_member_id: str
    created_at: str
    updated_at: str
    archived_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="direction_id"))
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "created_by_member_id",
            normalize_uuid(self.created_by_member_id, field_name="created_by_member_id"),
        )
        object.__setattr__(self, "title", normalize_direction_title(self.title))
        object.__setattr__(
            self,
            "description",
            normalize_direction_description(self.description),
        )


@dataclass(frozen=True, slots=True)
class ActivityDirectionBinding:
    business_id: str
    direction_id: str
    subject_kind: DirectionSubjectKind
    subject_id: str
    created_by_member_id: str
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "direction_id",
            normalize_uuid(self.direction_id, field_name="direction_id"),
        )
        object.__setattr__(
            self,
            "subject_id",
            normalize_uuid(self.subject_id, field_name="subject_id"),
        )
        object.__setattr__(
            self,
            "created_by_member_id",
            normalize_uuid(self.created_by_member_id, field_name="created_by_member_id"),
        )
        object.__setattr__(
            self,
            "subject_kind",
            normalize_direction_subject_kind(self.subject_kind),
        )


__all__ = [
    "ActivityDirection",
    "ActivityDirectionBinding",
    "ActivityDirectionError",
    "ActivityDirectionInvariantViolation",
    "ActivityDirectionNotFound",
    "ActivityDirectionStatus",
    "DirectionSubjectKind",
    "normalize_direction_description",
    "normalize_direction_subject_kind",
    "normalize_direction_title",
]
