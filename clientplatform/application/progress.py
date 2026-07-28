from __future__ import annotations

from clientplatform.domain.program_progress import (
    CustomerLessonCompletion,
    CustomerProgramView,
    ProgramProgressSummary,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.customer_progress_repository import CustomerProgressRepository
from clientplatform.infrastructure.program_progress_repository import ProgramProgressRepository
from services.db import get_db, get_db_ro


def list_customer_programs(
    *,
    telegram_user_id: int,
    business_id: str,
) -> list[ProgramProgressSummary]:
    with get_db_ro() as conn:
        return ProgramProgressRepository(conn).list_customer_programs(
            telegram_user_id=telegram_user_id,
            business_id=business_id,
        )


def get_customer_program(
    *,
    telegram_user_id: int,
    business_id: str,
    enrollment_id: str,
) -> CustomerProgramView:
    with get_db_ro() as conn:
        return ProgramProgressRepository(conn).get_customer_program(
            telegram_user_id=telegram_user_id,
            business_id=business_id,
            enrollment_id=enrollment_id,
        )


def complete_customer_lesson(
    *,
    telegram_user_id: int,
    business_id: str,
    enrollment_id: str,
    lesson_position: int,
) -> CustomerLessonCompletion:
    with get_db() as conn:
        return CustomerProgressRepository(conn).complete_lesson(
            telegram_user_id=telegram_user_id,
            business_id=business_id,
            enrollment_id=enrollment_id,
            lesson_position=lesson_position,
        )


def list_business_program_progress(
    *,
    actor: TenantContext,
    limit: int = 25,
) -> list[ProgramProgressSummary]:
    with get_db_ro() as conn:
        return ProgramProgressRepository(conn).list_business_progress(
            actor=actor,
            limit=limit,
        )
