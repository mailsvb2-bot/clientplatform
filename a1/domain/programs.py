from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from a1.domain.tenancy import normalize_uuid


class ProgramError(RuntimeError):
    """Base error for A1 programs and delivery state."""


class ProgramNotFound(ProgramError):
    """A program or lesson was not found in the active business."""


class ProgramInvariantViolation(ProgramError):
    """A program transition would violate a domain invariant."""


class EnrollmentNotFound(ProgramError):
    """An enrollment was not found in the active business."""


class DeliveryNotFound(ProgramError):
    """A lesson delivery was not found in the active business."""


class DeliveryInvariantViolation(ProgramError):
    """A delivery or progress transition is not allowed."""


class ProgramStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class LessonStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ContentKind(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"
    TEXT = "text"
    DOCUMENT = "document"
    IMAGE = "image"
    LINK = "link"
    TASK = "task"
    MIXED = "mixed"


class EnrollmentStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProgressStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    OPENED = "opened"
    COMPLETED = "completed"
    SKIPPED = "skipped"


def _normalize_text(
    value: str,
    *,
    field_name: str,
    maximum: int,
) -> str:
    raw = str(value or "").replace("\x00", " ")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return normalized


def normalize_program_title(value: str) -> str:
    return _normalize_text(
        value,
        field_name="program title",
        maximum=200,
    )


def normalize_lesson_title(value: str) -> str:
    return _normalize_text(
        value,
        field_name="lesson title",
        maximum=200,
    )


def normalize_content_kind(value: ContentKind | str) -> ContentKind:
    try:
        if isinstance(value, ContentKind):
            return value
        return ContentKind(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unsupported content kind: {value!r}") from exc


def normalize_content_ref(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("content_ref must not be empty")
    if len(normalized) > 2048:
        raise ValueError("content_ref must be at most 2048 characters")
    if any(ord(char) < 32 for char in normalized):
        raise ValueError("content_ref contains control characters")
    return normalized


def normalize_position(value: int) -> int:
    if isinstance(value, bool):
        raise ValueError("lesson position must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("lesson position must be a positive integer") from exc
    if normalized <= 0:
        raise ValueError("lesson position must be a positive integer")
    return normalized


@dataclass(frozen=True, slots=True)
class Program:
    id: str
    business_id: str
    title: str
    status: ProgramStatus
    created_by_member_id: str
    created_at: str
    updated_at: str
    published_at: str | None = None
    archived_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "id",
            normalize_uuid(self.id, field_name="program_id"),
        )
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "created_by_member_id",
            normalize_uuid(
                self.created_by_member_id,
                field_name="created_by_member_id",
            ),
        )


@dataclass(frozen=True, slots=True)
class Lesson:
    id: str
    business_id: str
    program_id: str
    position: int
    title: str
    content_kind: ContentKind
    content_ref: str
    status: LessonStatus
    created_at: str
    updated_at: str
    archived_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "id",
            normalize_uuid(self.id, field_name="lesson_id"),
        )
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "program_id",
            normalize_uuid(self.program_id, field_name="program_id"),
        )
        object.__setattr__(self, "position", normalize_position(self.position))
        object.__setattr__(
            self,
            "content_kind",
            normalize_content_kind(self.content_kind),
        )
        object.__setattr__(
            self,
            "content_ref",
            normalize_content_ref(self.content_ref),
        )


@dataclass(frozen=True, slots=True)
class ProgramRecord:
    program: Program
    lessons: tuple[Lesson, ...]


@dataclass(frozen=True, slots=True)
class Enrollment:
    id: str
    business_id: str
    program_id: str
    customer_id: str
    status: EnrollmentStatus
    started_at: str
    updated_at: str
    completed_at: str | None = None
    paused_at: str | None = None
    cancelled_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "id",
            normalize_uuid(self.id, field_name="enrollment_id"),
        )
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "program_id",
            normalize_uuid(self.program_id, field_name="program_id"),
        )
        object.__setattr__(
            self,
            "customer_id",
            normalize_uuid(self.customer_id, field_name="customer_id"),
        )


@dataclass(frozen=True, slots=True)
class LessonDelivery:
    id: str
    business_id: str
    program_id: str
    enrollment_id: str
    lesson_id: str
    idempotency_key: str
    status: DeliveryStatus
    scheduled_at: str
    attempts: int
    created_at: str
    updated_at: str
    sent_at: str | None = None
    failed_at: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class LessonProgress:
    id: str
    business_id: str
    program_id: str
    enrollment_id: str
    lesson_id: str
    status: ProgressStatus
    updated_at: str
    delivered_at: str | None = None
    opened_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True, slots=True)
class EnrollmentRecord:
    enrollment: Enrollment
    progress: tuple[LessonProgress, ...]
    deliveries: tuple[LessonDelivery, ...]
