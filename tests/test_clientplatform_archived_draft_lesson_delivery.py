from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.programs import DeliveryStatus, EnrollmentStatus, ProgressStatus
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.delivery_repository import DeliveryRepository
from clientplatform.infrastructure.program_draft_repository import ProgramDraftRepository
from clientplatform.infrastructure.program_repository import ProgramRepository
from services.db.schema import (
    clientplatform_customers,
    clientplatform_programs,
    clientplatform_tenancy,
)


class ClientPlatformArchivedDraftLessonDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_programs.ensure(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        self.customers = CustomerRepository(self.conn)
        self.programs = ProgramRepository(self.conn)
        self.drafts = ProgramDraftRepository(self.conn)
        self.delivery = DeliveryRepository(self.conn)

        business = self.tenancy.create_business(
            owner_user_id=101,
            name="Практика Марии",
        )
        self.owner = self.tenancy.resolve_context(
            user_id=101,
            business_id=business.business.id,
        )
        self.customer = self.customers.create_customer(
            actor=self.owner,
            display_name="Клиент Марии",
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_archived_draft_lesson_never_enters_delivery_or_progress(self) -> None:
        program = self.programs.create_program(
            actor=self.owner,
            title="Курс с исправленным составом",
        )
        lessons = [
            self.programs.add_lesson(
                actor=self.owner,
                program_id=program.id,
                title=f"Урок {index}",
                content_kind="text",
                content_ref=f"Материал {index}",
            )
            for index in range(1, 4)
        ]
        record_after_delete = self.drafts.archive_lesson(
            actor=self.owner,
            lesson_id=lessons[1].id,
            now="2026-07-30T14:00:00+00:00",
        )
        self.assertEqual(
            [item.id for item in record_after_delete.lessons],
            [lessons[0].id, lessons[2].id],
        )
        self.assertEqual(
            [item.position for item in record_after_delete.lessons],
            [1, 2],
        )

        self.programs.publish_program(
            actor=self.owner,
            program_id=program.id,
        )
        enrollment = self.delivery.enroll_customer(
            actor=self.owner,
            program_id=program.id,
            customer_id=self.customer.id,
        )
        self.assertEqual(len(enrollment.deliveries), 1)
        self.assertEqual(enrollment.deliveries[0].lesson_id, lessons[0].id)
        self.assertEqual(enrollment.deliveries[0].status, DeliveryStatus.PENDING)

        self.delivery.mark_delivery_sent(
            actor=self.owner,
            delivery_id=enrollment.deliveries[0].id,
        )
        advanced = self.delivery.complete_lesson(
            actor=self.owner,
            enrollment_id=enrollment.enrollment.id,
            lesson_id=lessons[0].id,
        )
        self.assertEqual(advanced.enrollment.status, EnrollmentStatus.ACTIVE)
        self.assertEqual(
            {item.lesson_id for item in advanced.deliveries},
            {lessons[0].id, lessons[2].id},
        )
        self.assertEqual(
            {item.lesson_id for item in advanced.progress},
            {lessons[0].id, lessons[2].id},
        )
        self.assertNotIn(
            lessons[1].id,
            {item.lesson_id for item in advanced.deliveries},
        )
        self.assertNotIn(
            lessons[1].id,
            {item.lesson_id for item in advanced.progress},
        )
        self.assertEqual(
            {item.status for item in advanced.progress},
            {ProgressStatus.COMPLETED, ProgressStatus.PENDING},
        )


if __name__ == "__main__":
    unittest.main()
