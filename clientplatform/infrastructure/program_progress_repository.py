from __future__ import annotations

from typing import Any

from clientplatform.domain.program_progress import (
    CustomerLessonProgressView,
    CustomerProgramView,
    ProgramProgressSummary,
)
from clientplatform.domain.programs import (
    DeliveryStatus,
    EnrollmentNotFound,
    EnrollmentStatus,
    ProgressStatus,
)
from clientplatform.domain.tenancy import TenantContext, normalize_user_id, normalize_uuid
from clientplatform.infrastructure import TenancyRepository


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _summary_from_row(row: Any) -> ProgramProgressSummary:
    display_name = _value(row, "customer_display_name", 3)
    return ProgramProgressSummary(
        business_id=str(_value(row, "business_id", 0)),
        business_name=str(_value(row, "business_name", 1)),
        customer_id=str(_value(row, "customer_id", 2)),
        customer_display_name=None if display_name is None else str(display_name),
        enrollment_id=str(_value(row, "enrollment_id", 4)),
        program_id=str(_value(row, "program_id", 5)),
        program_title=str(_value(row, "program_title", 6)),
        enrollment_status=EnrollmentStatus(str(_value(row, "enrollment_status", 7))),
        completed_lessons=int(_value(row, "completed_lessons", 8) or 0),
        total_lessons=int(_value(row, "total_lessons", 9) or 0),
        updated_at=str(_value(row, "updated_at", 10)),
    )


_SUMMARY_SELECT = """
    SELECT e.business_id, b.name AS business_name, e.customer_id,
           c.display_name AS customer_display_name, e.id AS enrollment_id,
           e.program_id, p.title AS program_title,
           e.status AS enrollment_status,
           SUM(CASE WHEN lp.status='completed' THEN 1 ELSE 0 END) AS completed_lessons,
           COUNT(l.id) AS total_lessons,
           MAX(COALESCE(lp.updated_at, e.updated_at)) AS updated_at
    FROM enrollments e
    JOIN businesses b ON b.id=e.business_id AND b.status='active'
    JOIN customers c ON c.id=e.customer_id AND c.business_id=e.business_id
    JOIN programs p ON p.id=e.program_id AND p.business_id=e.business_id
    LEFT JOIN lessons l
      ON l.business_id=e.business_id AND l.program_id=e.program_id AND l.status='active'
    LEFT JOIN lesson_progress lp
      ON lp.business_id=e.business_id AND lp.enrollment_id=e.id AND lp.lesson_id=l.id
"""

_SUMMARY_GROUP = """
    GROUP BY e.business_id, b.name, e.customer_id, c.display_name,
             e.id, e.program_id, p.title, e.status
"""


class ProgramProgressRepository:
    """Read customer and owner progress without trusting callback tenant IDs."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def resolve_customer_scope(self, *, telegram_user_id: int, business_id: str) -> tuple[str, str]:
        principal = normalize_user_id(telegram_user_id)
        normalized_business = normalize_uuid(business_id, field_name="business_id")
        row = self._conn.execute(
            """
            SELECT ci.business_id, ci.customer_id
            FROM customer_identities ci
            JOIN customers c
              ON c.id=ci.customer_id AND c.business_id=ci.business_id AND c.status='active'
            JOIN businesses b ON b.id=ci.business_id AND b.status='active'
            WHERE ci.business_id=? AND ci.platform='telegram'
              AND ci.external_subject=? AND ci.status='active'
            LIMIT 1
            """,
            (normalized_business, str(principal)),
        ).fetchone()
        if row is None:
            raise EnrollmentNotFound("Вы не подключены к этому бизнесу")
        return str(_value(row, "business_id", 0)), str(_value(row, "customer_id", 1))

    def resolve_customer_id_scope(
        self,
        *,
        business_id: str,
        customer_id: str,
    ) -> tuple[str, str]:
        business = normalize_uuid(business_id, field_name="business_id")
        customer = normalize_uuid(customer_id, field_name="customer_id")
        row = self._conn.execute(
            """
            SELECT c.business_id, c.id
            FROM customers c
            JOIN businesses b ON b.id=c.business_id AND b.status='active'
            WHERE c.business_id=? AND c.id=? AND c.status='active'
            LIMIT 1
            """,
            (business, customer),
        ).fetchone()
        if row is None:
            raise EnrollmentNotFound("Вы не подключены к этому бизнесу")
        return str(_value(row, "business_id", 0)), str(_value(row, "id", 1))

    def list_customer_programs(
        self,
        *,
        telegram_user_id: int,
        business_id: str,
    ) -> list[ProgramProgressSummary]:
        scoped_business, customer_id = self.resolve_customer_scope(
            telegram_user_id=telegram_user_id,
            business_id=business_id,
        )
        return self.list_customer_programs_by_customer(
            business_id=scoped_business, customer_id=customer_id
        )

    def list_customer_programs_by_customer(
        self,
        *,
        business_id: str,
        customer_id: str,
    ) -> list[ProgramProgressSummary]:
        scoped_business, customer = self.resolve_customer_id_scope(
            business_id=business_id, customer_id=customer_id
        )
        rows = self._conn.execute(
            _SUMMARY_SELECT
            + " WHERE e.business_id=? AND e.customer_id=? AND e.status!='cancelled' "
            + _SUMMARY_GROUP
            + " ORDER BY updated_at DESC, e.id LIMIT 50",
            (scoped_business, customer),
        ).fetchall()
        return [_summary_from_row(row) for row in rows]

    def get_customer_program(
        self,
        *,
        telegram_user_id: int,
        business_id: str,
        enrollment_id: str,
    ) -> CustomerProgramView:
        scoped_business, customer_id = self.resolve_customer_scope(
            telegram_user_id=telegram_user_id,
            business_id=business_id,
        )
        return self.get_customer_program_by_customer(
            business_id=scoped_business,
            customer_id=customer_id,
            enrollment_id=enrollment_id,
        )

    def get_customer_program_by_customer(
        self,
        *,
        business_id: str,
        customer_id: str,
        enrollment_id: str,
    ) -> CustomerProgramView:
        scoped_business, customer = self.resolve_customer_id_scope(
            business_id=business_id, customer_id=customer_id
        )
        normalized_enrollment = normalize_uuid(enrollment_id, field_name="enrollment_id")
        row = self._conn.execute(
            _SUMMARY_SELECT
            + " WHERE e.business_id=? AND e.customer_id=? AND e.id=? AND e.status!='cancelled' "
            + _SUMMARY_GROUP
            + " LIMIT 1",
            (scoped_business, customer, normalized_enrollment),
        ).fetchone()
        if row is None:
            raise EnrollmentNotFound("программа клиента не найдена")
        summary = _summary_from_row(row)
        lesson_rows = self._conn.execute(
            """
            SELECT l.id, l.position, l.title,
                   COALESCE(lp.status, 'pending') AS progress_status,
                   COALESCE(ld.status, 'pending') AS delivery_status
            FROM lessons l
            LEFT JOIN lesson_progress lp
              ON lp.business_id=l.business_id AND lp.enrollment_id=? AND lp.lesson_id=l.id
            LEFT JOIN lesson_deliveries ld
              ON ld.business_id=l.business_id AND ld.enrollment_id=? AND ld.lesson_id=l.id
            WHERE l.business_id=? AND l.program_id=? AND l.status='active'
            ORDER BY l.position, l.id
            """,
            (normalized_enrollment, normalized_enrollment, scoped_business, summary.program_id),
        ).fetchall()
        lessons = tuple(
            CustomerLessonProgressView(
                lesson_id=str(_value(item, "id", 0)),
                position=int(_value(item, "position", 1)),
                title=str(_value(item, "title", 2)),
                progress_status=ProgressStatus(str(_value(item, "progress_status", 3))),
                delivery_status=DeliveryStatus(str(_value(item, "delivery_status", 4))),
            )
            for item in lesson_rows
        )
        return CustomerProgramView(summary=summary, lessons=lessons)

    def list_business_progress(
        self,
        *,
        actor: TenantContext,
        limit: int = 25,
    ) -> list[ProgramProgressSummary]:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_customer_records()
        safe_limit = max(1, min(int(limit), 100))
        rows = self._conn.execute(
            _SUMMARY_SELECT
            + " WHERE e.business_id=? AND e.status!='cancelled' "
            + _SUMMARY_GROUP
            + " ORDER BY updated_at DESC, e.id LIMIT ?",
            (current.business_id, safe_limit),
        ).fetchall()
        return [_summary_from_row(row) for row in rows]
