from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.programs import (
    ProgramInvariantViolation,
    ProgramNotFound,
    ProgramStatus,
)
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.program_draft_repository import ProgramDraftRepository
from clientplatform.infrastructure.program_repository import ProgramRepository
from services.db.schema import clientplatform_programs, clientplatform_tenancy


class ClientPlatformPersistentProgramDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_programs.ensure(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        self.programs = ProgramRepository(self.conn)
        self.drafts = ProgramDraftRepository(self.conn)

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

    def tearDown(self) -> None:
        self.conn.close()

    def _draft(self, *, title: str = "Черновик"):
        program = self.programs.create_program(
            actor=self.owner_a,
            title=title,
        )
        lesson = self.programs.add_lesson(
            actor=self.owner_a,
            program_id=program.id,
            title="Урок 1",
            content_kind="text",
            content_ref="Сохранённый материал",
        )
        return program, lesson

    def test_draft_is_listed_and_can_be_resumed_with_ordered_lessons(self) -> None:
        program, lesson = self._draft()

        listed = self.drafts.list_drafts(actor=self.owner_a)
        record = self.drafts.get_draft(
            actor=self.owner_a,
            program_id=program.id,
        )

        self.assertEqual([item.id for item in listed], [program.id])
        self.assertEqual(record.program.status, ProgramStatus.DRAFT)
        self.assertEqual([item.id for item in record.lessons], [lesson.id])
        self.assertEqual([item.position for item in record.lessons], [1])

    def test_content_manager_can_resume_but_support_cannot(self) -> None:
        program, _lesson = self._draft()
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
        manager = self.tenancy.resolve_context(
            user_id=303,
            business_id=self.business_a.business.id,
        )
        support = self.tenancy.resolve_context(
            user_id=404,
            business_id=self.business_a.business.id,
        )

        self.assertEqual(
            self.drafts.get_draft(actor=manager, program_id=program.id).program.id,
            program.id,
        )
        with self.assertRaises(TenantPermissionDenied):
            self.drafts.list_drafts(actor=support)
        with self.assertRaises(TenantPermissionDenied):
            self.drafts.get_draft(actor=support, program_id=program.id)

    def test_archiving_draft_archives_lessons_and_hides_it(self) -> None:
        program, lesson = self._draft()

        archived = self.drafts.archive_draft(
            actor=self.owner_a,
            program_id=program.id,
            now="2026-07-30T12:00:00+00:00",
        )
        program_row = self.conn.execute(
            "SELECT status, archived_at FROM programs WHERE id=?",
            (program.id,),
        ).fetchone()
        lesson_row = self.conn.execute(
            "SELECT status, archived_at FROM lessons WHERE id=?",
            (lesson.id,),
        ).fetchone()

        self.assertEqual(archived.status, ProgramStatus.ARCHIVED)
        self.assertEqual(program_row["status"], "archived")
        self.assertEqual(lesson_row["status"], "archived")
        self.assertEqual(program_row["archived_at"], "2026-07-30T12:00:00+00:00")
        self.assertEqual(lesson_row["archived_at"], "2026-07-30T12:00:00+00:00")
        self.assertEqual(self.drafts.list_drafts(actor=self.owner_a), [])
        self.assertEqual(self.programs.list_programs(actor=self.owner_a), [])

    def test_active_and_cross_tenant_programs_fail_closed(self) -> None:
        program, _lesson = self._draft()
        self.programs.publish_program(actor=self.owner_a, program_id=program.id)

        with self.assertRaises(ProgramInvariantViolation):
            self.drafts.archive_draft(
                actor=self.owner_a,
                program_id=program.id,
            )
        with self.assertRaises(ProgramInvariantViolation):
            self.drafts.get_draft(
                actor=self.owner_a,
                program_id=program.id,
            )
        with self.assertRaises(ProgramNotFound):
            self.drafts.get_draft(
                actor=self.owner_b,
                program_id=program.id,
            )


if __name__ == "__main__":
    unittest.main()
