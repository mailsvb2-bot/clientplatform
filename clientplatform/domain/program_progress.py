from __future__ import annotations

from dataclasses import dataclass

from clientplatform.domain.programs import (
    DeliveryStatus,
    EnrollmentStatus,
    ProgressStatus,
    normalize_position,
)
from clientplatform.domain.tenancy import normalize_uuid


def _clean_text(value: str, *, field_name: str) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


@dataclass(frozen=True, slots=True)
class ProgramProgressSummary:
    business_id: str
    business_name: str
    customer_id: str
    customer_display_name: str | None
    enrollment_id: str
    program_id: str
    program_title: str
    enrollment_status: EnrollmentStatus
    completed_lessons: int
    total_lessons: int
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(self, "customer_id", normalize_uuid(self.customer_id, field_name="customer_id"))
        object.__setattr__(
            self,
            "enrollment_id",
            normalize_uuid(self.enrollment_id, field_name="enrollment_id"),
        )
        object.__setattr__(self, "program_id", normalize_uuid(self.program_id, field_name="program_id"))
        object.__setattr__(
            self,
            "business_name",
            _clean_text(self.business_name, field_name="business_name"),
        )
        object.__setattr__(
            self,
            "program_title",
            _clean_text(self.program_title, field_name="program_title"),
        )
        if self.customer_display_name is not None:
            object.__setattr__(
                self,
                "customer_display_name",
                _clean_text(self.customer_display_name, field_name="customer_display_name"),
            )
        completed = int(self.completed_lessons)
        total = int(self.total_lessons)
        if completed < 0 or total < 0 or completed > total:
            raise ValueError("invalid lesson progress counters")
        object.__setattr__(self, "completed_lessons", completed)
        object.__setattr__(self, "total_lessons", total)

    @property
    def percent_complete(self) -> int:
        if self.total_lessons == 0:
            return 0
        return round(self.completed_lessons * 100 / self.total_lessons)


@dataclass(frozen=True, slots=True)
class CustomerLessonProgressView:
    lesson_id: str
    position: int
    title: str
    progress_status: ProgressStatus
    delivery_status: DeliveryStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "lesson_id", normalize_uuid(self.lesson_id, field_name="lesson_id"))
        object.__setattr__(self, "position", normalize_position(self.position))
        object.__setattr__(self, "title", _clean_text(self.title, field_name="lesson_title"))

    @property
    def can_complete(self) -> bool:
        return self.progress_status in {ProgressStatus.DELIVERED, ProgressStatus.OPENED}


@dataclass(frozen=True, slots=True)
class CustomerProgramView:
    summary: ProgramProgressSummary
    lessons: tuple[CustomerLessonProgressView, ...]


@dataclass(frozen=True, slots=True)
class CustomerLessonCompletion:
    program: CustomerProgramView
    next_material_queued: bool
