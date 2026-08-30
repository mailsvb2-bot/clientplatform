from __future__ import annotations

from clientplatform.domain.programs import (
    ContentKind,
    EnrollmentRecord,
    Lesson,
    LessonDelivery,
    Program,
    ProgramRecord,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.delivery_repository import DeliveryRepository
from clientplatform.infrastructure.program_draft_repository import ProgramDraftRepository
from clientplatform.infrastructure.program_repository import ProgramRepository
from services.db import get_db, get_db_ro


def create_program(
    *,
    actor: TenantContext,
    title: str,
    idempotency_key: str | None = None,
) -> Program:
    with get_db() as conn:
        return ProgramRepository(conn).create_program(
            actor=actor,
            title=title,
            idempotency_key=idempotency_key,
        )


def add_program_lesson(
    *,
    actor: TenantContext,
    program_id: str,
    title: str,
    content_kind: ContentKind | str,
    content_ref: str,
    position: int | None = None,
    idempotency_key: str | None = None,
) -> Lesson:
    with get_db() as conn:
        return ProgramRepository(conn).add_lesson(
            actor=actor,
            program_id=program_id,
            title=title,
            content_kind=content_kind,
            content_ref=content_ref,
            position=position,
            idempotency_key=idempotency_key,
        )


def publish_program(
    *,
    actor: TenantContext,
    program_id: str,
) -> Program:
    with get_db() as conn:
        return ProgramRepository(conn).publish_program(
            actor=actor,
            program_id=program_id,
        )


def archive_program_draft(
    *,
    actor: TenantContext,
    program_id: str,
) -> Program:
    with get_db() as conn:
        return ProgramDraftRepository(conn).archive_draft(
            actor=actor,
            program_id=program_id,
        )


def get_program(
    *,
    actor: TenantContext,
    program_id: str,
) -> ProgramRecord:
    with get_db_ro() as conn:
        return ProgramRepository(conn).get_program(
            actor=actor,
            program_id=program_id,
        )


def get_program_draft(
    *,
    actor: TenantContext,
    program_id: str,
) -> ProgramRecord:
    with get_db_ro() as conn:
        return ProgramDraftRepository(conn).get_draft(
            actor=actor,
            program_id=program_id,
        )


def get_program_draft_lesson(
    *,
    actor: TenantContext,
    lesson_id: str,
) -> tuple[ProgramRecord, Lesson]:
    with get_db_ro() as conn:
        return ProgramDraftRepository(conn).get_lesson_draft(
            actor=actor,
            lesson_id=lesson_id,
        )


def update_program_draft_lesson_title(
    *,
    actor: TenantContext,
    lesson_id: str,
    title: str,
) -> tuple[ProgramRecord, Lesson]:
    with get_db() as conn:
        return ProgramDraftRepository(conn).update_lesson_title(
            actor=actor,
            lesson_id=lesson_id,
            title=title,
        )


def replace_program_draft_lesson_content(
    *,
    actor: TenantContext,
    lesson_id: str,
    content_kind: ContentKind | str,
    content_ref: str,
) -> tuple[ProgramRecord, Lesson]:
    with get_db() as conn:
        return ProgramDraftRepository(conn).replace_lesson_content(
            actor=actor,
            lesson_id=lesson_id,
            content_kind=content_kind,
            content_ref=content_ref,
        )


def move_program_draft_lesson(
    *,
    actor: TenantContext,
    lesson_id: str,
    direction: str,
) -> ProgramRecord:
    with get_db() as conn:
        return ProgramDraftRepository(conn).move_lesson(
            actor=actor,
            lesson_id=lesson_id,
            direction=direction,
        )


def archive_program_draft_lesson(
    *,
    actor: TenantContext,
    lesson_id: str,
) -> ProgramRecord:
    with get_db() as conn:
        return ProgramDraftRepository(conn).archive_lesson(
            actor=actor,
            lesson_id=lesson_id,
        )


def list_programs(
    *,
    actor: TenantContext,
    include_archived: bool = False,
) -> list[Program]:
    with get_db_ro() as conn:
        return ProgramRepository(conn).list_programs(
            actor=actor,
            include_archived=include_archived,
        )


def list_program_drafts(
    *,
    actor: TenantContext,
) -> list[Program]:
    with get_db_ro() as conn:
        return ProgramDraftRepository(conn).list_drafts(actor=actor)


def enroll_customer_in_program(
    *,
    actor: TenantContext,
    program_id: str,
    customer_id: str,
) -> EnrollmentRecord:
    with get_db() as conn:
        return DeliveryRepository(conn).enroll_customer(
            actor=actor,
            program_id=program_id,
            customer_id=customer_id,
        )


def get_program_enrollment(
    *,
    actor: TenantContext,
    enrollment_id: str,
) -> EnrollmentRecord:
    with get_db_ro() as conn:
        return DeliveryRepository(conn).get_enrollment(
            actor=actor,
            enrollment_id=enrollment_id,
        )


def mark_lesson_delivery_sent(
    *,
    actor: TenantContext,
    delivery_id: str,
) -> LessonDelivery:
    with get_db() as conn:
        return DeliveryRepository(conn).mark_delivery_sent(
            actor=actor,
            delivery_id=delivery_id,
        )


def mark_lesson_delivery_failed(
    *,
    actor: TenantContext,
    delivery_id: str,
    error: str,
) -> LessonDelivery:
    with get_db() as conn:
        return DeliveryRepository(conn).mark_delivery_failed(
            actor=actor,
            delivery_id=delivery_id,
            error=error,
        )


def complete_program_lesson(
    *,
    actor: TenantContext,
    enrollment_id: str,
    lesson_id: str,
) -> EnrollmentRecord:
    with get_db() as conn:
        return DeliveryRepository(conn).complete_lesson(
            actor=actor,
            enrollment_id=enrollment_id,
            lesson_id=lesson_id,
        )
