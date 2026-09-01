from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.programs import (
    DeliveryInvariantViolation,
    DeliveryStatus,
    EnrollmentNotFound,
    EnrollmentStatus,
    ProgressStatus,
    ProgramInvariantViolation,
    ProgramNotFound,
    ProgramStatus,
)
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.delivery_repository import DeliveryRepository
from clientplatform.infrastructure.program_repository import ProgramRepository
from clientplatform.privacy_manifest import validate_clientplatform_privacy_manifest
from services.db.schema import clientplatform_customers, clientplatform_programs, clientplatform_tenancy


class ClientPlatformProgramDeliveryVerticalTests(unittest.TestCase):
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
        self.delivery = DeliveryRepository(self.conn)

        self.business_a = self.tenancy.create_business(
            owner_user_id=101,
            name="Практика Марии",
        )
        self.business_b = self.tenancy.create_business(
            owner_user_id=202,
            name="Школа Нины",
        )
        self.owner_a = self.tenancy.resolve_context(
            user_id=101,
            business_id=self.business_a.business.id,
        )
        self.owner_b = self.tenancy.resolve_context(
            user_id=202,
            business_id=self.business_b.business.id,
        )
        self.customer_a = self.customers.create_customer(
            actor=self.owner_a,
            display_name="Клиент Марии",
        )
        self.customer_b = self.customers.create_customer(
            actor=self.owner_b,
            display_name="Клиент Нины",
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _published_program(
        self,
        *,
        actor=None,
        title: str = "Спокойный сон",
        lessons: int = 2,
    ):
        active_actor = actor or self.owner_a
        program = self.programs.create_program(
            actor=active_actor,
            title=title,
        )
        created_lessons = []
        for position in range(1, lessons + 1):
            created_lessons.append(
                self.programs.add_lesson(
                    actor=active_actor,
                    program_id=program.id,
                    title=f"Урок {position}",
                    content_kind="audio",
                    content_ref=f"s3://clientplatform/lesson-{position}.mp3",
                )
            )
        published = self.programs.publish_program(
            actor=active_actor,
            program_id=program.id,
        )
        return published, created_lessons

    def test_content_manager_can_build_program_but_support_cannot(self) -> None:
        self.tenancy.grant_member(
            actor=self.owner_a,
            user_id=303,
            role=PlatformRole.CONTENT_MANAGER,
        )
        self.tenancy.grant_member(
            actor=self.owner_a,
            user_id=404,
            role=PlatformRole.SUPPORT,
        )
        content_manager = self.tenancy.resolve_context(
            user_id=303,
            business_id=self.business_a.business.id,
        )
        support = self.tenancy.resolve_context(
            user_id=404,
            business_id=self.business_a.business.id,
        )
        program = self.programs.create_program(
            actor=content_manager,
            title="Курс специалиста",
        )
        self.assertEqual(program.status, ProgramStatus.DRAFT)
        with self.assertRaises(TenantPermissionDenied):
            self.programs.create_program(
                actor=support,
                title="Запрещённый курс",
            )

    def test_program_cannot_publish_without_active_lesson(self) -> None:
        program = self.programs.create_program(
            actor=self.owner_a,
            title="Пустая программа",
        )
        with self.assertRaises(ProgramInvariantViolation):
            self.programs.publish_program(
                actor=self.owner_a,
                program_id=program.id,
            )

    def test_published_program_is_ordered_and_immutable_for_lesson_addition(self) -> None:
        program, lessons = self._published_program(lessons=2)
        record = self.programs.get_program(
            actor=self.owner_a,
            program_id=program.id,
        )
        self.assertEqual(program.status, ProgramStatus.ACTIVE)
        self.assertEqual([lesson.position for lesson in record.lessons], [1, 2])
        self.assertEqual([lesson.id for lesson in record.lessons], [item.id for item in lessons])
        with self.assertRaises(ProgramInvariantViolation):
            self.programs.add_lesson(
                actor=self.owner_a,
                program_id=program.id,
                title="Поздний урок",
                content_kind="text",
                content_ref="Поздний текст",
            )

    def test_draft_program_and_archived_customer_cannot_enroll(self) -> None:
        draft = self.programs.create_program(
            actor=self.owner_a,
            title="Черновик",
        )
        with self.assertRaises(ProgramInvariantViolation):
            self.delivery.enroll_customer(
                actor=self.owner_a,
                program_id=draft.id,
                customer_id=self.customer_a.id,
            )
        program, _lessons = self._published_program()
        self.customers.archive_customer(
            actor=self.owner_a,
            customer_id=self.customer_a.id,
        )
        with self.assertRaises(EnrollmentNotFound):
            self.delivery.enroll_customer(
                actor=self.owner_a,
                program_id=program.id,
                customer_id=self.customer_a.id,
            )

    def test_enrollment_schedules_first_lesson_exactly_once(self) -> None:
        program, lessons = self._published_program()
        first = self.delivery.enroll_customer(
            actor=self.owner_a,
            program_id=program.id,
            customer_id=self.customer_a.id,
        )
        repeated = self.delivery.enroll_customer(
            actor=self.owner_a,
            program_id=program.id,
            customer_id=self.customer_a.id,
        )
        self.assertEqual(first.enrollment.id, repeated.enrollment.id)
        self.assertEqual(len(repeated.deliveries), 1)
        self.assertEqual(len(repeated.progress), 1)
        self.assertEqual(repeated.deliveries[0].lesson_id, lessons[0].id)
        self.assertEqual(repeated.deliveries[0].status, DeliveryStatus.PENDING)
        self.assertEqual(repeated.progress[0].status, ProgressStatus.PENDING)

    def test_sent_and_completion_advance_program_idempotently(self) -> None:
        program, lessons = self._published_program(lessons=2)
        enrollment = self.delivery.enroll_customer(
            actor=self.owner_a,
            program_id=program.id,
            customer_id=self.customer_a.id,
        )
        first_delivery = enrollment.deliveries[0]
        sent = self.delivery.mark_delivery_sent(
            actor=self.owner_a,
            delivery_id=first_delivery.id,
        )
        sent_again = self.delivery.mark_delivery_sent(
            actor=self.owner_a,
            delivery_id=first_delivery.id,
        )
        self.assertEqual(sent.status, DeliveryStatus.SENT)
        self.assertEqual(sent_again.attempts, 1)

        advanced = self.delivery.complete_lesson(
            actor=self.owner_a,
            enrollment_id=enrollment.enrollment.id,
            lesson_id=lessons[0].id,
        )
        repeated = self.delivery.complete_lesson(
            actor=self.owner_a,
            enrollment_id=enrollment.enrollment.id,
            lesson_id=lessons[0].id,
        )
        self.assertEqual(advanced.enrollment.status, EnrollmentStatus.ACTIVE)
        self.assertEqual(len(advanced.deliveries), 2)
        self.assertEqual(len(repeated.deliveries), 2)
        self.assertEqual(
            {item.lesson_id for item in repeated.deliveries},
            {lessons[0].id, lessons[1].id},
        )

    def test_last_lesson_completion_completes_enrollment(self) -> None:
        program, lessons = self._published_program(lessons=1)
        record = self.delivery.enroll_customer(
            actor=self.owner_a,
            program_id=program.id,
            customer_id=self.customer_a.id,
        )
        self.delivery.mark_delivery_sent(
            actor=self.owner_a,
            delivery_id=record.deliveries[0].id,
        )
        completed = self.delivery.complete_lesson(
            actor=self.owner_a,
            enrollment_id=record.enrollment.id,
            lesson_id=lessons[0].id,
        )
        repeated = self.delivery.complete_lesson(
            actor=self.owner_a,
            enrollment_id=record.enrollment.id,
            lesson_id=lessons[0].id,
        )
        self.assertEqual(completed.enrollment.status, EnrollmentStatus.COMPLETED)
        self.assertIsNotNone(completed.enrollment.completed_at)
        self.assertEqual(repeated.enrollment.status, EnrollmentStatus.COMPLETED)
        self.assertEqual(len(repeated.deliveries), 1)

    def test_lesson_cannot_complete_before_delivery(self) -> None:
        program, lessons = self._published_program(lessons=1)
        record = self.delivery.enroll_customer(
            actor=self.owner_a,
            program_id=program.id,
            customer_id=self.customer_a.id,
        )
        with self.assertRaises(DeliveryInvariantViolation):
            self.delivery.complete_lesson(
                actor=self.owner_a,
                enrollment_id=record.enrollment.id,
                lesson_id=lessons[0].id,
            )

    def test_failed_delivery_can_retry_without_duplicate_progress(self) -> None:
        program, _lessons = self._published_program(lessons=1)
        record = self.delivery.enroll_customer(
            actor=self.owner_a,
            program_id=program.id,
            customer_id=self.customer_a.id,
        )
        failed = self.delivery.mark_delivery_failed(
            actor=self.owner_a,
            delivery_id=record.deliveries[0].id,
            error="temporary network",
        )
        sent = self.delivery.mark_delivery_sent(
            actor=self.owner_a,
            delivery_id=failed.id,
        )
        final_record = self.delivery.get_enrollment(
            actor=self.owner_a,
            enrollment_id=record.enrollment.id,
        )
        self.assertEqual(failed.status, DeliveryStatus.FAILED)
        self.assertEqual(sent.status, DeliveryStatus.SENT)
        self.assertEqual(sent.attempts, 2)
        self.assertEqual(len(final_record.progress), 1)
        self.assertEqual(final_record.progress[0].status, ProgressStatus.DELIVERED)

    def test_cross_business_program_and_enrollment_are_invisible(self) -> None:
        program, _lessons = self._published_program()
        record = self.delivery.enroll_customer(
            actor=self.owner_a,
            program_id=program.id,
            customer_id=self.customer_a.id,
        )
        with self.assertRaises(ProgramNotFound):
            self.programs.get_program(
                actor=self.owner_b,
                program_id=program.id,
            )
        with self.assertRaises(EnrollmentNotFound):
            self.delivery.get_enrollment(
                actor=self.owner_b,
                enrollment_id=record.enrollment.id,
            )
        with self.assertRaises(ProgramInvariantViolation):
            self.delivery.enroll_customer(
                actor=self.owner_b,
                program_id=program.id,
                customer_id=self.customer_b.id,
            )

    def test_database_rejects_cross_program_delivery_link(self) -> None:
        program_a, _lessons_a = self._published_program(
            title="Программа А",
            lessons=1,
        )
        program_b, lessons_b = self._published_program(
            title="Программа Б",
            lessons=1,
        )
        record = self.delivery.enroll_customer(
            actor=self.owner_a,
            program_id=program_a.id,
            customer_id=self.customer_a.id,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO lesson_deliveries(
                    id, business_id, program_id, enrollment_id, lesson_id,
                    idempotency_key, status, scheduled_at, attempts,
                    sent_at, failed_at, last_error, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, 0, NULL, NULL, NULL, ?, ?)
                """,
                (
                    "68ac680d-9913-44ef-bdc6-647f65da5dac",
                    self.business_a.business.id,
                    program_a.id,
                    record.enrollment.id,
                    lessons_b[0].id,
                    "cross-program",
                    "2026-07-28T00:00:00+00:00",
                    "2026-07-28T00:00:00+00:00",
                    "2026-07-28T00:00:00+00:00",
                ),
            )
        self.assertNotEqual(program_a.id, program_b.id)

    def test_privacy_manifest_covers_complete_vertical(self) -> None:
        report = validate_clientplatform_privacy_manifest(self.conn, strict=True)
        self.assertTrue(report.ok)
        self.assertEqual(
            set(report.discovered_business_tables),
            {
                "business_members",
                "clientplatform_owner_control_workspaces",
                "clientplatform_owner_onboarding_sessions",
                "customers",
                "customer_identities",
                "programs",
                "lessons",
                "enrollments",
                "lesson_deliveries",
                "lesson_progress",
            },
        )


if __name__ == "__main__":
    unittest.main()
