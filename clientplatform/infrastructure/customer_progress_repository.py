from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.program_progress import CustomerLessonCompletion
from clientplatform.domain.programs import (
    DeliveryInvariantViolation,
    EnrollmentNotFound,
    EnrollmentStatus,
    ProgressStatus,
    normalize_position,
)
from clientplatform.domain.tenancy import normalize_uuid
from clientplatform.infrastructure.program_progress_repository import ProgramProgressRepository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


class CustomerProgressRepository:
    """Customer-authorized lesson completion with automatic next dispatch."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._read = ProgramProgressRepository(conn)

    def complete_lesson(
        self,
        *,
        telegram_user_id: int,
        business_id: str,
        enrollment_id: str,
        lesson_position: int,
        now: str | None = None,
    ) -> CustomerLessonCompletion:
        scoped_business, customer_id = self._read.resolve_customer_scope(
            telegram_user_id=telegram_user_id,
            business_id=business_id,
        )
        return self.complete_lesson_by_customer(
            business_id=scoped_business,
            customer_id=customer_id,
            enrollment_id=enrollment_id,
            lesson_position=lesson_position,
            now=now,
        )

    def complete_lesson_by_customer(
        self,
        *,
        business_id: str,
        customer_id: str,
        enrollment_id: str,
        lesson_position: int,
        now: str | None = None,
    ) -> CustomerLessonCompletion:
        scoped_business, customer_id = self._read.resolve_customer_id_scope(
            business_id=business_id, customer_id=customer_id
        )
        enrollment_id = normalize_uuid(enrollment_id, field_name="enrollment_id")
        position = normalize_position(lesson_position)
        timestamp = str(now or _utc_now())
        row = self._conn.execute(
            """
            SELECT e.program_id, e.status AS enrollment_status,
                   l.id AS lesson_id, l.position,
                   lp.status AS progress_status, ld.id AS delivery_id
            FROM enrollments e
            JOIN lessons l
              ON l.business_id=e.business_id AND l.program_id=e.program_id
             AND l.position=? AND l.status='active'
            JOIN lesson_progress lp
              ON lp.business_id=e.business_id AND lp.enrollment_id=e.id AND lp.lesson_id=l.id
            JOIN lesson_deliveries ld
              ON ld.business_id=e.business_id AND ld.enrollment_id=e.id AND ld.lesson_id=l.id
            WHERE e.id=? AND e.business_id=? AND e.customer_id=?
              AND e.status!='cancelled'
            LIMIT 1
            """,
            (position, enrollment_id, scoped_business, customer_id),
        ).fetchone()
        if row is None:
            raise EnrollmentNotFound("урок программы клиента не найден")
        program_id = str(_value(row, "program_id", 0))
        enrollment_status = EnrollmentStatus(str(_value(row, "enrollment_status", 1)))
        lesson_id = str(_value(row, "lesson_id", 2))
        progress_status = ProgressStatus(str(_value(row, "progress_status", 4)))
        delivery_id = str(_value(row, "delivery_id", 5))
        if progress_status == ProgressStatus.COMPLETED:
            return CustomerLessonCompletion(
                program=self._read.get_customer_program_by_customer(
                    business_id=scoped_business,
                    customer_id=customer_id,
                    enrollment_id=enrollment_id,
                ),
                next_material_queued=self._has_next_dispatch(
                    business_id=scoped_business,
                    enrollment_id=enrollment_id,
                    program_id=program_id,
                    position=position,
                ),
            )
        if enrollment_status != EnrollmentStatus.ACTIVE:
            raise DeliveryInvariantViolation("завершение урока требует активной программы")
        if progress_status not in {ProgressStatus.DELIVERED, ProgressStatus.OPENED}:
            raise DeliveryInvariantViolation("материал должен быть доставлен до завершения")

        next_lesson = self._conn.execute(
            """
            SELECT id, content_kind, content_ref
            FROM lessons
            WHERE business_id=? AND program_id=? AND status='active' AND position>?
            ORDER BY position, id LIMIT 1
            """,
            (scoped_business, program_id, position),
        ).fetchone()
        route = None
        if next_lesson is not None:
            route = self._dispatch_route(
                business_id=scoped_business,
                customer_id=customer_id,
                delivery_id=delivery_id,
            )
            if route is None:
                raise DeliveryInvariantViolation("не удалось подготовить отправку следующего материала")

        self._conn.execute(
            """
            UPDATE lesson_progress
            SET status='completed', completed_at=?, updated_at=?
            WHERE business_id=? AND enrollment_id=? AND lesson_id=?
              AND status IN ('delivered', 'opened')
            """,
            (timestamp, timestamp, scoped_business, enrollment_id, lesson_id),
        )
        queued = False
        if next_lesson is None:
            self._conn.execute(
                """
                UPDATE enrollments
                SET status='completed', completed_at=?, updated_at=?
                WHERE id=? AND business_id=? AND customer_id=? AND status='active'
                """,
                (timestamp, timestamp, enrollment_id, scoped_business, customer_id),
            )
        else:
            next_id = str(_value(next_lesson, "id", 0))
            logical_delivery_id = self._ensure_delivery(
                business_id=scoped_business,
                program_id=program_id,
                enrollment_id=enrollment_id,
                lesson_id=next_id,
                now=timestamp,
            )
            connection_id, identity_id, platform = route
            self._ensure_dispatch(
                business_id=scoped_business,
                logical_delivery_id=logical_delivery_id,
                connection_id=connection_id,
                identity_id=identity_id,
                platform=platform,
                content_kind=str(_value(next_lesson, "content_kind", 1)),
                content_ref=str(_value(next_lesson, "content_ref", 2)),
                now=timestamp,
            )
            queued = True
        return CustomerLessonCompletion(
            program=self._read.get_customer_program_by_customer(
                business_id=scoped_business,
                customer_id=customer_id,
                enrollment_id=enrollment_id,
            ),
            next_material_queued=queued,
        )

    def _dispatch_route(self, *, business_id: str, customer_id: str, delivery_id: str) -> tuple[str, str, str] | None:
        row = self._conn.execute(
            """
            SELECT o.connection_id, o.customer_identity_id, o.platform
            FROM delivery_dispatch_outbox o
            JOIN connections c
              ON c.id=o.connection_id AND c.business_id=o.business_id AND c.status='active'
            JOIN customer_identities ci
              ON ci.id=o.customer_identity_id AND ci.business_id=o.business_id
             AND ci.customer_id=? AND ci.status='active'
            WHERE o.business_id=? AND o.logical_delivery_id=?
              AND o.status IN ('pending', 'sending', 'retry', 'sent')
            ORDER BY CASE WHEN o.status='sent' THEN 0 ELSE 1 END, o.created_at DESC
            LIMIT 1
            """,
            (customer_id, business_id, delivery_id),
        ).fetchone()
        if row is None:
            return None
        return str(_value(row, "connection_id", 0)), str(_value(row, "customer_identity_id", 1)), str(_value(row, "platform", 2))

    def _ensure_delivery(self, *, business_id: str, program_id: str, enrollment_id: str, lesson_id: str, now: str) -> str:
        existing = self._conn.execute(
            "SELECT id FROM lesson_deliveries WHERE business_id=? AND enrollment_id=? AND lesson_id=? LIMIT 1",
            (business_id, enrollment_id, lesson_id),
        ).fetchone()
        if existing is not None:
            delivery_id = str(_value(existing, "id", 0))
        else:
            delivery_id = str(uuid4())
            try:
                self._conn.execute(
                    """
                    INSERT INTO lesson_deliveries(
                        id, business_id, program_id, enrollment_id, lesson_id,
                        idempotency_key, status, scheduled_at, attempts, sent_at,
                        failed_at, last_error, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, 0, NULL, NULL, NULL, ?, ?)
                    """,
                    (delivery_id, business_id, program_id, enrollment_id, lesson_id,
                     f"enrollment:{enrollment_id}:lesson:{lesson_id}", now, now, now),
                )
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT id FROM lesson_deliveries WHERE business_id=? AND enrollment_id=? AND lesson_id=? LIMIT 1",
                    (business_id, enrollment_id, lesson_id),
                ).fetchone()
                if row is None:
                    raise
                delivery_id = str(_value(row, "id", 0))
        progress = self._conn.execute(
            "SELECT id FROM lesson_progress WHERE business_id=? AND enrollment_id=? AND lesson_id=? LIMIT 1",
            (business_id, enrollment_id, lesson_id),
        ).fetchone()
        if progress is None:
            try:
                self._conn.execute(
                    """
                    INSERT INTO lesson_progress(
                        id, business_id, program_id, enrollment_id, lesson_id,
                        status, delivered_at, opened_at, completed_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?)
                    """,
                    (str(uuid4()), business_id, program_id, enrollment_id, lesson_id, now),
                )
            except sqlite3.IntegrityError:
                concurrent = self._conn.execute(
                    "SELECT id FROM lesson_progress WHERE business_id=? AND enrollment_id=? AND lesson_id=? LIMIT 1",
                    (business_id, enrollment_id, lesson_id),
                ).fetchone()
                if concurrent is None:
                    raise
        return delivery_id

    def _ensure_dispatch(self, *, business_id: str, logical_delivery_id: str, connection_id: str, identity_id: str, platform: str, content_kind: str, content_ref: str, now: str) -> None:
        existing = self._conn.execute(
            """
            SELECT id FROM delivery_dispatch_outbox
            WHERE business_id=? AND logical_delivery_id=? AND connection_id=?
              AND customer_identity_id=? LIMIT 1
            """,
            (business_id, logical_delivery_id, connection_id, identity_id),
        ).fetchone()
        if existing is not None:
            return
        self._conn.execute(
            """
            INSERT INTO delivery_dispatch_outbox(
                id, business_id, platform, logical_delivery_id, connection_id,
                customer_identity_id, payload_kind, payload_ref, idempotency_key,
                status, attempts, available_at, locked_at, lock_token,
                provider_message_id, last_error, created_at, updated_at, sent_at, dead_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL,
                     NULL, NULL, ?, ?, NULL, NULL)
            ON CONFLICT(
                business_id, logical_delivery_id, connection_id, customer_identity_id
            ) DO NOTHING
            """,
            (str(uuid4()), business_id, platform, logical_delivery_id, connection_id,
             identity_id, content_kind, content_ref,
             f"delivery:{logical_delivery_id}:connection:{connection_id}:identity:{identity_id}",
             now, now, now),
        )

    def _has_next_dispatch(self, *, business_id: str, enrollment_id: str, program_id: str, position: int) -> bool:
        row = self._conn.execute(
            """
            SELECT o.id
            FROM lessons l
            JOIN lesson_deliveries d
              ON d.business_id=l.business_id AND d.enrollment_id=? AND d.lesson_id=l.id
            JOIN delivery_dispatch_outbox o
              ON o.business_id=d.business_id AND o.logical_delivery_id=d.id
            WHERE l.business_id=? AND l.program_id=? AND l.status='active' AND l.position>?
            LIMIT 1
            """,
            (enrollment_id, business_id, program_id, position),
        ).fetchone()
        return row is not None
