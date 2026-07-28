from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.connections import ConnectionPlatform, ConnectionType
from clientplatform.domain.programs import EnrollmentNotFound, EnrollmentStatus, ProgressStatus
from clientplatform.infrastructure import (
    ConnectionRepository,
    DispatchOutboxRepository,
    TenancyRepository,
)
from clientplatform.infrastructure.customer_progress_repository import CustomerProgressRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.delivery_repository import DeliveryRepository
from clientplatform.infrastructure.program_progress_repository import ProgramProgressRepository
from clientplatform.infrastructure.program_repository import ProgramRepository
from clientplatform.runtime.control_bot import CONTROL_BOT_CREDENTIAL_REFERENCE
from services.db.schema import (
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_programs,
    clientplatform_tenancy,
)


class ClientPlatformProgramProgressPortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_programs.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        self.customers = CustomerRepository(self.conn)
        self.programs = ProgramRepository(self.conn)
        self.deliveries = DeliveryRepository(self.conn)
        self.connections = ConnectionRepository(self.conn)
        self.outbox = DispatchOutboxRepository(self.conn)
        self.read = ProgramProgressRepository(self.conn)
        self.write = CustomerProgressRepository(self.conn)

        access = self.tenancy.create_business(owner_user_id=101, name="Практика Марии")
        self.business_id = access.business.id
        self.owner = self.tenancy.resolve_context(user_id=101, business_id=self.business_id)
        customer = self.customers.create_customer(actor=self.owner, display_name="Анна Клиент")
        self.customer_id = customer.id
        self.identity = self.customers.attach_identity(
            actor=self.owner,
            customer_id=customer.id,
            platform="telegram",
            external_subject="700001",
            username="anna",
            display_name="Анна Клиент",
            now="2026-07-28T12:00:00+00:00",
        )
        connection = self.connections.create_connection(
            actor=self.owner,
            platform=ConnectionPlatform.TELEGRAM,
            connection_type=ConnectionType.TELEGRAM_SHARED_BOT,
            external_account_id="123456",
            credential_reference=CONTROL_BOT_CREDENTIAL_REFERENCE,
            permissions=("send_message", "send_media"),
            now="2026-07-28T12:00:00+00:00",
        )
        self.connection = self.connections.activate_connection(
            actor=self.owner,
            connection_id=connection.id,
            now="2026-07-28T12:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _enroll(self, lesson_count: int):
        program = self.programs.create_program(
            actor=self.owner,
            title=f"Программа из {lesson_count} уроков",
            now="2026-07-28T12:00:00+00:00",
        )
        lessons = []
        for position in range(1, lesson_count + 1):
            lessons.append(
                self.programs.add_lesson(
                    actor=self.owner,
                    program_id=program.id,
                    title=f"Урок {position}",
                    content_kind="text",
                    content_ref=f"Материал {position}",
                    position=position,
                    now="2026-07-28T12:00:00+00:00",
                )
            )
        self.programs.publish_program(
            actor=self.owner,
            program_id=program.id,
            now="2026-07-28T12:00:00+00:00",
        )
        enrollment = self.deliveries.enroll_customer(
            actor=self.owner,
            program_id=program.id,
            customer_id=self.customer_id,
            now="2026-07-28T12:01:00+00:00",
        )
        first = enrollment.deliveries[0]
        self.outbox.materialize(
            actor=self.owner,
            logical_delivery_id=first.id,
            connection_id=self.connection.id,
            customer_identity_id=self.identity.id,
            now="2026-07-28T12:02:00+00:00",
        )
        self.deliveries.mark_delivery_sent(
            actor=self.owner,
            delivery_id=first.id,
            now="2026-07-28T12:03:00+00:00",
        )
        return program, lessons, enrollment

    def test_customer_completes_program_and_owner_sees_named_progress(self) -> None:
        _program, _lessons, enrollment = self._enroll(1)
        summaries = self.read.list_customer_programs(
            telegram_user_id=700001,
            business_id=self.business_id,
        )
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].completed_lessons, 0)
        self.assertEqual(summaries[0].total_lessons, 1)

        completed = self.write.complete_lesson(
            telegram_user_id=700001,
            business_id=self.business_id,
            enrollment_id=enrollment.enrollment.id,
            lesson_position=1,
            now="2026-07-28T12:04:00+00:00",
        )
        self.assertFalse(completed.next_material_queued)
        self.assertEqual(completed.program.summary.completed_lessons, 1)
        self.assertEqual(completed.program.summary.enrollment_status, EnrollmentStatus.COMPLETED)
        self.assertEqual(completed.program.lessons[0].progress_status, ProgressStatus.COMPLETED)

        repeated = self.write.complete_lesson(
            telegram_user_id=700001,
            business_id=self.business_id,
            enrollment_id=enrollment.enrollment.id,
            lesson_position=1,
            now="2026-07-28T12:05:00+00:00",
        )
        self.assertEqual(repeated.program.summary.completed_lessons, 1)
        owner_view = self.read.list_business_progress(actor=self.owner)
        self.assertEqual(owner_view[0].customer_display_name, "Анна Клиент")
        self.assertEqual(owner_view[0].percent_complete, 100)

    def test_completion_queues_next_lesson_once_with_same_route(self) -> None:
        _program, _lessons, enrollment = self._enroll(2)
        result = self.write.complete_lesson(
            telegram_user_id=700001,
            business_id=self.business_id,
            enrollment_id=enrollment.enrollment.id,
            lesson_position=1,
            now="2026-07-28T12:04:00+00:00",
        )
        self.assertTrue(result.next_material_queued)
        self.assertEqual(result.program.summary.completed_lessons, 1)
        self.assertEqual(result.program.summary.enrollment_status, EnrollmentStatus.ACTIVE)
        self.assertEqual(result.program.lessons[1].progress_status, ProgressStatus.PENDING)
        counts = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM delivery_dispatch_outbox o
            JOIN lesson_deliveries d ON d.id=o.logical_delivery_id
            WHERE o.business_id=? AND d.enrollment_id=?
            """,
            (self.business_id, enrollment.enrollment.id),
        ).fetchone()
        self.assertEqual(int(counts["c"]), 2)
        self.write.complete_lesson(
            telegram_user_id=700001,
            business_id=self.business_id,
            enrollment_id=enrollment.enrollment.id,
            lesson_position=1,
            now="2026-07-28T12:05:00+00:00",
        )
        repeated = self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM delivery_dispatch_outbox o
            JOIN lesson_deliveries d ON d.id=o.logical_delivery_id
            WHERE o.business_id=? AND d.enrollment_id=?
            """,
            (self.business_id, enrollment.enrollment.id),
        ).fetchone()
        self.assertEqual(int(repeated["c"]), 2)

    def test_foreign_or_unlinked_customer_cannot_read_or_complete(self) -> None:
        _program, _lessons, enrollment = self._enroll(1)
        with self.assertRaises(EnrollmentNotFound):
            self.read.get_customer_program(
                telegram_user_id=999999,
                business_id=self.business_id,
                enrollment_id=enrollment.enrollment.id,
            )
        with self.assertRaises(EnrollmentNotFound):
            self.write.complete_lesson(
                telegram_user_id=999999,
                business_id=self.business_id,
                enrollment_id=enrollment.enrollment.id,
                lesson_position=1,
                now="2026-07-28T12:04:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()
