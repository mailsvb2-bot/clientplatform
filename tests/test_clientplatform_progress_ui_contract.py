from __future__ import annotations

import unittest
from pathlib import Path
from uuid import uuid4

from clientplatform.domain.program_progress import (
    CustomerLessonProgressView,
    ProgramProgressSummary,
)
from clientplatform.domain.programs import (
    DeliveryStatus,
    EnrollmentStatus,
    ProgressStatus,
)


class ClientPlatformProgressUiContractTests(unittest.TestCase):
    def test_progress_percentage_is_integer_and_zero_safe(self) -> None:
        summary = ProgramProgressSummary(
            business_id=str(uuid4()),
            business_name="Практика",
            customer_id=str(uuid4()),
            customer_display_name="Клиент",
            enrollment_id=str(uuid4()),
            program_id=str(uuid4()),
            program_title="Программа",
            enrollment_status=EnrollmentStatus.ACTIVE,
            completed_lessons=1,
            total_lessons=3,
            updated_at="2026-07-28T18:00:00+00:00",
        )
        self.assertEqual(summary.percent_complete, 33)

        empty = ProgramProgressSummary(
            business_id=str(uuid4()),
            business_name="Практика",
            customer_id=str(uuid4()),
            customer_display_name=None,
            enrollment_id=str(uuid4()),
            program_id=str(uuid4()),
            program_title="Пустая программа",
            enrollment_status=EnrollmentStatus.ACTIVE,
            completed_lessons=0,
            total_lessons=0,
            updated_at="2026-07-28T18:00:00+00:00",
        )
        self.assertEqual(empty.percent_complete, 0)

    def test_only_delivered_or_opened_lesson_can_show_completion_action(self) -> None:
        common = {
            "lesson_id": str(uuid4()),
            "position": 1,
            "title": "Первый урок",
            "delivery_status": DeliveryStatus.SENT,
        }
        delivered = CustomerLessonProgressView(
            **common,
            progress_status=ProgressStatus.DELIVERED,
        )
        opened = CustomerLessonProgressView(
            **common,
            progress_status=ProgressStatus.OPENED,
        )
        pending = CustomerLessonProgressView(
            **common,
            progress_status=ProgressStatus.PENDING,
        )
        completed = CustomerLessonProgressView(
            **common,
            progress_status=ProgressStatus.COMPLETED,
        )
        self.assertTrue(delivered.can_complete)
        self.assertTrue(opened.can_complete)
        self.assertFalse(pending.can_complete)
        self.assertFalse(completed.can_complete)

    def test_handler_exposes_customer_and_owner_progress_surfaces(self) -> None:
        source = Path("handlers/clientplatform_control.py").read_text(encoding="utf-8")
        self.assertIn("Мои программы", source)
        self.assertIn("Готово · урок", source)
        self.assertIn("Прогресс клиентов", source)
        self.assertIn("next_material_queued", source)


if __name__ == "__main__":
    unittest.main()
