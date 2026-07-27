from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from a1.domain.programs import (
    DeliveryInvariantViolation,
    DeliveryNotFound,
    DeliveryStatus,
    Enrollment,
    EnrollmentNotFound,
    EnrollmentRecord,
    EnrollmentStatus,
    LessonDelivery,
    LessonProgress,
    ProgressStatus,
    ProgramInvariantViolation,
)
from a1.domain.tenancy import TenantContext, normalize_uuid
from a1.infrastructure import TenancyRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _enrollment_from_row(row: Any) -> Enrollment:
    completed_at = _value(row, "completed_at", 7)
    paused_at = _value(row, "paused_at", 8)
    cancelled_at = _value(row, "cancelled_at", 9)
    return Enrollment(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        program_id=str(_value(row, "program_id", 2)),
        customer_id=str(_value(row, "customer_id", 3)),
        status=EnrollmentStatus(str(_value(row, "status", 4))),
        started_at=str(_value(row, "started_at", 5)),
        updated_at=str(_value(row, "updated_at", 6)),
        completed_at=None if completed_at is None else str(completed_at),
        paused_at=None if paused_at is None else str(paused_at),
        cancelled_at=None if cancelled_at is None else str(cancelled_at),
    )


def _delivery_from_row(row: Any) -> LessonDelivery:
    sent_at = _value(row, "sent_at", 10)
    failed_at = _value(row, "failed_at", 11)
    last_error = _value(row, "last_error", 12)
    return LessonDelivery(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        program_id=str(_value(row, "program_id", 2)),
        enrollment_id=str(_value(row, "enrollment_id", 3)),
        lesson_id=str(_value(row, "lesson_id", 4)),
        idempotency_key=str(_value(row, "idempotency_key", 5)),
        status=DeliveryStatus(str(_value(row, "status", 6))),
        scheduled_at=str(_value(row, "scheduled_at", 7)),
        attempts=int(_value(row, "attempts", 8)),
        created_at=str(_value(row, "created_at", 9)),
        updated_at=str(_value(row, "updated_at", 13)),
        sent_at=None if sent_at is None else str(sent_at),
        failed_at=None if failed_at is None else str(failed_at),
        last_error=None if last_error is None else str(last_error),
    )


def _progress_from_row(row: Any) -> LessonProgress:
    delivered_at = _value(row, "delivered_at", 6)
    opened_at = _value(row, "opened_at", 7)
    completed_at = _value(row, "completed_at", 8)
    return LessonProgress(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        program_id=str(_value(row, "program_id", 2)),
        enrollment_id=str(_value(row, "enrollment_id", 3)),
        lesson_id=str(_value(row, "lesson_id", 4)),
        status=ProgressStatus(str(_value(row, "status", 5))),
        delivered_at=None if delivered_at is None else str(delivered_at),
        opened_at=None if opened_at is None else str(opened_at),
        completed_at=None if completed_at is None else str(completed_at),
        updated_at=str(_value(row, "updated_at", 9)),
    )


class DeliveryRepository:
    """Tenant-scoped enrollment, delivery and progress state machine."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def _resolve_actor(self, actor: TenantContext) -> TenantContext:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_deliveries()
        return current

    def enroll_customer(
        self,
        *,
        actor: TenantContext,
        program_id: str,
        customer_id: str,
        now: str | None = None,
    ) -> EnrollmentRecord:
        current = self._resolve_actor(actor)
        normalized_program_id = normalize_uuid(
            program_id,
            field_name="program_id",
        )
        normalized_customer_id = normalize_uuid(
            customer_id,
            field_name="customer_id",
        )
        timestamp = str(now or _utc_now())

        self._require_active_program(
            business_id=current.business_id,
            program_id=normalized_program_id,
        )
        self._require_active_customer(
            business_id=current.business_id,
            customer_id=normalized_customer_id,
        )
        first_lesson = self._first_active_lesson(
            business_id=current.business_id,
            program_id=normalized_program_id,
        )
        if first_lesson is None:
            raise ProgramInvariantViolation(
                "an active program must contain an active lesson"
            )

        existing = self._find_enrollment(
            business_id=current.business_id,
            program_id=normalized_program_id,
            customer_id=normalized_customer_id,
        )
        if existing is not None:
            if existing.status == EnrollmentStatus.CANCELLED:
                raise DeliveryInvariantViolation(
                    "a cancelled enrollment requires an explicit restart workflow"
                )
            if existing.status in {
                EnrollmentStatus.ACTIVE,
                EnrollmentStatus.PAUSED,
            }:
                self._ensure_delivery(
                    business_id=current.business_id,
                    enrollment=existing,
                    lesson_id=str(_value(first_lesson, "id", 0)),
                    scheduled_at=timestamp,
                    now=timestamp,
                )
            return self.get_enrollment(
                actor=current,
                enrollment_id=existing.id,
            )

        enrollment_id = str(uuid4())
        try:
            self._conn.execute(
                """
                INSERT INTO enrollments(
                    id, business_id, program_id, customer_id, status,
                    started_at, updated_at, completed_at, paused_at, cancelled_at
                ) VALUES(?, ?, ?, ?, 'active', ?, ?, NULL, NULL, NULL)
                """,
                (
                    enrollment_id,
                    current.business_id,
                    normalized_program_id,
                    normalized_customer_id,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            concurrent = self._find_enrollment(
                business_id=current.business_id,
                program_id=normalized_program_id,
                customer_id=normalized_customer_id,
            )
            if concurrent is None:
                raise
            if concurrent.status == EnrollmentStatus.CANCELLED:
                raise DeliveryInvariantViolation(
                    "a cancelled enrollment requires an explicit restart workflow"
                ) from exc
            enrollment = concurrent
        else:
            enrollment = self._get_enrollment_row(
                business_id=current.business_id,
                enrollment_id=enrollment_id,
            )

        self._ensure_delivery(
            business_id=current.business_id,
            enrollment=enrollment,
            lesson_id=str(_value(first_lesson, "id", 0)),
            scheduled_at=timestamp,
            now=timestamp,
        )
        return self.get_enrollment(
            actor=current,
            enrollment_id=enrollment.id,
        )

    def get_enrollment(
        self,
        *,
        actor: TenantContext,
        enrollment_id: str,
    ) -> EnrollmentRecord:
        current = self._resolve_actor(actor)
        normalized_enrollment_id = normalize_uuid(
            enrollment_id,
            field_name="enrollment_id",
        )
        enrollment = self._get_enrollment_row(
            business_id=current.business_id,
            enrollment_id=normalized_enrollment_id,
        )
        progress_rows = self._conn.execute(
            """
            SELECT id, business_id, program_id, enrollment_id, lesson_id,
                   status, delivered_at, opened_at, completed_at, updated_at
            FROM lesson_progress
            WHERE business_id=? AND enrollment_id=?
            ORDER BY updated_at, id
            """,
            (current.business_id, normalized_enrollment_id),
        ).fetchall()
        delivery_rows = self._conn.execute(
            """
            SELECT id, business_id, program_id, enrollment_id, lesson_id,
                   idempotency_key, status, scheduled_at, attempts, created_at,
                   sent_at, failed_at, last_error, updated_at
            FROM lesson_deliveries
            WHERE business_id=? AND enrollment_id=?
            ORDER BY scheduled_at, id
            """,
            (current.business_id, normalized_enrollment_id),
        ).fetchall()
        return EnrollmentRecord(
            enrollment=enrollment,
            progress=tuple(_progress_from_row(row) for row in progress_rows),
            deliveries=tuple(_delivery_from_row(row) for row in delivery_rows),
        )

    def mark_delivery_sent(
        self,
        *,
        actor: TenantContext,
        delivery_id: str,
        now: str | None = None,
    ) -> LessonDelivery:
        current = self._resolve_actor(actor)
        normalized_delivery_id = normalize_uuid(
            delivery_id,
            field_name="delivery_id",
        )
        timestamp = str(now or _utc_now())
        delivery = self._get_delivery(
            business_id=current.business_id,
            delivery_id=normalized_delivery_id,
        )
        if delivery.status == DeliveryStatus.SENT:
            return delivery
        if delivery.status == DeliveryStatus.CANCELLED:
            raise DeliveryInvariantViolation(
                "a cancelled delivery cannot be sent"
            )
        enrollment = self._get_enrollment_row(
            business_id=current.business_id,
            enrollment_id=delivery.enrollment_id,
        )
        if enrollment.status != EnrollmentStatus.ACTIVE:
            raise DeliveryInvariantViolation(
                "delivery requires an active enrollment"
            )
        self._conn.execute(
            """
            UPDATE lesson_deliveries
            SET status='sent', attempts=attempts+1, sent_at=?, failed_at=NULL,
                last_error=NULL, updated_at=?
            WHERE id=? AND business_id=? AND status IN ('pending', 'failed')
            """,
            (
                timestamp,
                timestamp,
                normalized_delivery_id,
                current.business_id,
            ),
        )
        self._set_progress_delivered(
            business_id=current.business_id,
            enrollment=enrollment,
            lesson_id=delivery.lesson_id,
            now=timestamp,
        )
        return self._get_delivery(
            business_id=current.business_id,
            delivery_id=normalized_delivery_id,
        )

    def mark_delivery_failed(
        self,
        *,
        actor: TenantContext,
        delivery_id: str,
        error: str,
        now: str | None = None,
    ) -> LessonDelivery:
        current = self._resolve_actor(actor)
        normalized_delivery_id = normalize_uuid(
            delivery_id,
            field_name="delivery_id",
        )
        normalized_error = str(error or "delivery_failed").strip()[:1000]
        timestamp = str(now or _utc_now())
        delivery = self._get_delivery(
            business_id=current.business_id,
            delivery_id=normalized_delivery_id,
        )
        if delivery.status == DeliveryStatus.SENT:
            return delivery
        if delivery.status == DeliveryStatus.CANCELLED:
            raise DeliveryInvariantViolation(
                "a cancelled delivery cannot fail"
            )
        self._conn.execute(
            """
            UPDATE lesson_deliveries
            SET status='failed', attempts=attempts+1, failed_at=?,
                last_error=?, updated_at=?
            WHERE id=? AND business_id=? AND status IN ('pending', 'failed')
            """,
            (
                timestamp,
                normalized_error,
                timestamp,
                normalized_delivery_id,
                current.business_id,
            ),
        )
        return self._get_delivery(
            business_id=current.business_id,
            delivery_id=normalized_delivery_id,
        )

    def complete_lesson(
        self,
        *,
        actor: TenantContext,
        enrollment_id: str,
        lesson_id: str,
        now: str | None = None,
    ) -> EnrollmentRecord:
        current = self._resolve_actor(actor)
        normalized_enrollment_id = normalize_uuid(
            enrollment_id,
            field_name="enrollment_id",
        )
        normalized_lesson_id = normalize_uuid(
            lesson_id,
            field_name="lesson_id",
        )
        timestamp = str(now or _utc_now())
        enrollment = self._get_enrollment_row(
            business_id=current.business_id,
            enrollment_id=normalized_enrollment_id,
        )
        progress = self._get_progress(
            business_id=current.business_id,
            enrollment_id=normalized_enrollment_id,
            lesson_id=normalized_lesson_id,
        )
        if progress.program_id != enrollment.program_id:
            raise DeliveryInvariantViolation(
                "lesson progress belongs to another program"
            )
        if progress.status == ProgressStatus.COMPLETED:
            return self._advance_after_completion(
                actor=current,
                enrollment=enrollment,
                lesson_id=normalized_lesson_id,
                now=timestamp,
            )
        if enrollment.status != EnrollmentStatus.ACTIVE:
            raise DeliveryInvariantViolation(
                "lesson completion requires an active enrollment"
            )
        if progress.status not in {
            ProgressStatus.DELIVERED,
            ProgressStatus.OPENED,
        }:
            raise DeliveryInvariantViolation(
                "a lesson must be delivered before completion"
            )
        self._conn.execute(
            """
            UPDATE lesson_progress
            SET status='completed', completed_at=?, updated_at=?
            WHERE business_id=? AND enrollment_id=? AND lesson_id=?
              AND status IN ('delivered', 'opened')
            """,
            (
                timestamp,
                timestamp,
                current.business_id,
                normalized_enrollment_id,
                normalized_lesson_id,
            ),
        )
        return self._advance_after_completion(
            actor=current,
            enrollment=enrollment,
            lesson_id=normalized_lesson_id,
            now=timestamp,
        )

    def _advance_after_completion(
        self,
        *,
        actor: TenantContext,
        enrollment: Enrollment,
        lesson_id: str,
        now: str,
    ) -> EnrollmentRecord:
        current_lesson = self._conn.execute(
            """
            SELECT position
            FROM lessons
            WHERE id=? AND business_id=? AND program_id=?
            LIMIT 1
            """,
            (
                lesson_id,
                actor.business_id,
                enrollment.program_id,
            ),
        ).fetchone()
        if current_lesson is None:
            raise DeliveryNotFound(
                "completed lesson was not found in the enrollment program"
            )
        position = int(_value(current_lesson, "position", 0))
        next_lesson = self._conn.execute(
            """
            SELECT id
            FROM lessons
            WHERE business_id=? AND program_id=? AND status='active'
              AND position>?
            ORDER BY position, id
            LIMIT 1
            """,
            (actor.business_id, enrollment.program_id, position),
        ).fetchone()
        if next_lesson is not None:
            if enrollment.status == EnrollmentStatus.COMPLETED:
                raise DeliveryInvariantViolation(
                    "completed enrollment cannot schedule another lesson"
                )
            self._ensure_delivery(
                business_id=actor.business_id,
                enrollment=enrollment,
                lesson_id=str(_value(next_lesson, "id", 0)),
                scheduled_at=now,
                now=now,
            )
        elif enrollment.status != EnrollmentStatus.COMPLETED:
            self._conn.execute(
                """
                UPDATE enrollments
                SET status='completed', completed_at=?, updated_at=?
                WHERE id=? AND business_id=? AND status='active'
                """,
                (
                    now,
                    now,
                    enrollment.id,
                    actor.business_id,
                ),
            )
        return self.get_enrollment(
            actor=actor,
            enrollment_id=enrollment.id,
        )

    def _ensure_delivery(
        self,
        *,
        business_id: str,
        enrollment: Enrollment,
        lesson_id: str,
        scheduled_at: str,
        now: str,
    ) -> LessonDelivery:
        normalized_lesson_id = normalize_uuid(
            lesson_id,
            field_name="lesson_id",
        )
        lesson = self._conn.execute(
            """
            SELECT id
            FROM lessons
            WHERE id=? AND business_id=? AND program_id=? AND status='active'
            LIMIT 1
            """,
            (
                normalized_lesson_id,
                business_id,
                enrollment.program_id,
            ),
        ).fetchone()
        if lesson is None:
            raise ProgramInvariantViolation(
                "active lesson was not found in the enrollment program"
            )
        existing = self._find_delivery(
            business_id=business_id,
            enrollment_id=enrollment.id,
            lesson_id=normalized_lesson_id,
        )
        if existing is not None:
            self._ensure_progress(
                business_id=business_id,
                enrollment=enrollment,
                lesson_id=normalized_lesson_id,
                now=now,
            )
            return existing

        delivery_id = str(uuid4())
        idempotency_key = (
            f"enrollment:{enrollment.id}:lesson:{normalized_lesson_id}"
        )
        try:
            self._conn.execute(
                """
                INSERT INTO lesson_deliveries(
                    id, business_id, program_id, enrollment_id, lesson_id,
                    idempotency_key, status, scheduled_at, attempts, sent_at,
                    failed_at, last_error, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, 0, NULL, NULL, NULL, ?, ?)
                """,
                (
                    delivery_id,
                    business_id,
                    enrollment.program_id,
                    enrollment.id,
                    normalized_lesson_id,
                    idempotency_key,
                    scheduled_at,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            concurrent = self._find_delivery(
                business_id=business_id,
                enrollment_id=enrollment.id,
                lesson_id=normalized_lesson_id,
            )
            if concurrent is None:
                raise
            delivery = concurrent
        else:
            delivery = self._get_delivery(
                business_id=business_id,
                delivery_id=delivery_id,
            )
        self._ensure_progress(
            business_id=business_id,
            enrollment=enrollment,
            lesson_id=normalized_lesson_id,
            now=now,
        )
        return delivery

    def _ensure_progress(
        self,
        *,
        business_id: str,
        enrollment: Enrollment,
        lesson_id: str,
        now: str,
    ) -> LessonProgress:
        existing = self._find_progress(
            business_id=business_id,
            enrollment_id=enrollment.id,
            lesson_id=lesson_id,
        )
        if existing is not None:
            return existing
        progress_id = str(uuid4())
        try:
            self._conn.execute(
                """
                INSERT INTO lesson_progress(
                    id, business_id, program_id, enrollment_id, lesson_id,
                    status, delivered_at, opened_at, completed_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?)
                """,
                (
                    progress_id,
                    business_id,
                    enrollment.program_id,
                    enrollment.id,
                    lesson_id,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            concurrent = self._find_progress(
                business_id=business_id,
                enrollment_id=enrollment.id,
                lesson_id=lesson_id,
            )
            if concurrent is None:
                raise
            return concurrent
        return self._get_progress(
            business_id=business_id,
            enrollment_id=enrollment.id,
            lesson_id=lesson_id,
        )

    def _set_progress_delivered(
        self,
        *,
        business_id: str,
        enrollment: Enrollment,
        lesson_id: str,
        now: str,
    ) -> LessonProgress:
        progress = self._ensure_progress(
            business_id=business_id,
            enrollment=enrollment,
            lesson_id=lesson_id,
            now=now,
        )
        if progress.status == ProgressStatus.PENDING:
            self._conn.execute(
                """
                UPDATE lesson_progress
                SET status='delivered', delivered_at=?, updated_at=?
                WHERE business_id=? AND enrollment_id=? AND lesson_id=?
                  AND status='pending'
                """,
                (
                    now,
                    now,
                    business_id,
                    enrollment.id,
                    lesson_id,
                ),
            )
        return self._get_progress(
            business_id=business_id,
            enrollment_id=enrollment.id,
            lesson_id=lesson_id,
        )

    def _require_active_program(
        self,
        *,
        business_id: str,
        program_id: str,
    ) -> None:
        row = self._conn.execute(
            """
            SELECT id
            FROM programs
            WHERE id=? AND business_id=? AND status='active'
            LIMIT 1
            """,
            (program_id, business_id),
        ).fetchone()
        if row is None:
            raise ProgramInvariantViolation(
                "customer enrollment requires an active program"
            )

    def _require_active_customer(
        self,
        *,
        business_id: str,
        customer_id: str,
    ) -> None:
        row = self._conn.execute(
            """
            SELECT id
            FROM customers
            WHERE id=? AND business_id=? AND status='active'
            LIMIT 1
            """,
            (customer_id, business_id),
        ).fetchone()
        if row is None:
            raise EnrollmentNotFound(
                "active customer was not found in the business"
            )

    def _first_active_lesson(
        self,
        *,
        business_id: str,
        program_id: str,
    ) -> Any | None:
        return self._conn.execute(
            """
            SELECT id, position
            FROM lessons
            WHERE business_id=? AND program_id=? AND status='active'
            ORDER BY position, id
            LIMIT 1
            """,
            (business_id, program_id),
        ).fetchone()

    def _find_enrollment(
        self,
        *,
        business_id: str,
        program_id: str,
        customer_id: str,
    ) -> Enrollment | None:
        row = self._conn.execute(
            """
            SELECT id, business_id, program_id, customer_id, status,
                   started_at, updated_at, completed_at, paused_at, cancelled_at
            FROM enrollments
            WHERE business_id=? AND program_id=? AND customer_id=?
            LIMIT 1
            """,
            (business_id, program_id, customer_id),
        ).fetchone()
        return None if row is None else _enrollment_from_row(row)

    def _get_enrollment_row(
        self,
        *,
        business_id: str,
        enrollment_id: str,
    ) -> Enrollment:
        row = self._conn.execute(
            """
            SELECT id, business_id, program_id, customer_id, status,
                   started_at, updated_at, completed_at, paused_at, cancelled_at
            FROM enrollments
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (enrollment_id, business_id),
        ).fetchone()
        if row is None:
            raise EnrollmentNotFound(
                "enrollment was not found in the active business"
            )
        return _enrollment_from_row(row)

    def _find_delivery(
        self,
        *,
        business_id: str,
        enrollment_id: str,
        lesson_id: str,
    ) -> LessonDelivery | None:
        row = self._conn.execute(
            """
            SELECT id, business_id, program_id, enrollment_id, lesson_id,
                   idempotency_key, status, scheduled_at, attempts, created_at,
                   sent_at, failed_at, last_error, updated_at
            FROM lesson_deliveries
            WHERE business_id=? AND enrollment_id=? AND lesson_id=?
            LIMIT 1
            """,
            (business_id, enrollment_id, lesson_id),
        ).fetchone()
        return None if row is None else _delivery_from_row(row)

    def _get_delivery(
        self,
        *,
        business_id: str,
        delivery_id: str,
    ) -> LessonDelivery:
        row = self._conn.execute(
            """
            SELECT id, business_id, program_id, enrollment_id, lesson_id,
                   idempotency_key, status, scheduled_at, attempts, created_at,
                   sent_at, failed_at, last_error, updated_at
            FROM lesson_deliveries
            WHERE id=? AND business_id=?
            LIMIT 1
            """,
            (delivery_id, business_id),
        ).fetchone()
        if row is None:
            raise DeliveryNotFound(
                "delivery was not found in the active business"
            )
        return _delivery_from_row(row)

    def _find_progress(
        self,
        *,
        business_id: str,
        enrollment_id: str,
        lesson_id: str,
    ) -> LessonProgress | None:
        row = self._conn.execute(
            """
            SELECT id, business_id, program_id, enrollment_id, lesson_id,
                   status, delivered_at, opened_at, completed_at, updated_at
            FROM lesson_progress
            WHERE business_id=? AND enrollment_id=? AND lesson_id=?
            LIMIT 1
            """,
            (business_id, enrollment_id, lesson_id),
        ).fetchone()
        return None if row is None else _progress_from_row(row)

    def _get_progress(
        self,
        *,
        business_id: str,
        enrollment_id: str,
        lesson_id: str,
    ) -> LessonProgress:
        progress = self._find_progress(
            business_id=business_id,
            enrollment_id=enrollment_id,
            lesson_id=lesson_id,
        )
        if progress is None:
            raise DeliveryNotFound(
                "lesson progress was not found in the enrollment"
            )
        return progress
