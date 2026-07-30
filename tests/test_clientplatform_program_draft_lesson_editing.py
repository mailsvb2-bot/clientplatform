from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.programs import (
    ContentKind,
    ProgramInvariantViolation,
    ProgramNotFound,
)
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.program_draft_repository import ProgramDraftRepository
from clientplatform.infrastructure.program_repository import ProgramRepository
from services.db.schema import clientplatform_programs, clientplatform_tenancy


class ClientPlatformProgramDraftLessonEditingTests(unittest.TestCase):
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

    def _draft_with_lessons(self, count: int = 4):
        program = self.programs.create_program(
            actor=self.owner_a,
            title="Редактируемый черновик",
        )
        lessons = [
            self.programs.add_lesson(
                actor=self.owner_a,
                program_id=program.id,
                title=f"Урок {index}",
                content_kind="text",
                content_ref=f"Материал {index}",
            )
            for index in range(1, count + 1)
        ]
        return program, lessons

    def test_rename_and_replace_material_keep_identity_and_position(self) -> None:
        _program, lessons = self._draft_with_lessons(2)
        target = lessons[1]

        renamed_record, renamed = self.drafts.update_lesson_title(
            actor=self.owner_a,
            lesson_id=target.id,
            title="  Новое   название  ",
            now="2026-07-30T13:30:00+00:00",
        )
        replaced_record, replaced = self.drafts.replace_lesson_content(
            actor=self.owner_a,
            lesson_id=target.id,
            content_kind=ContentKind.AUDIO,
            content_ref="telegram-audio-id",
            now="2026-07-30T13:31:00+00:00",
        )

        self.assertEqual(renamed.id, target.id)
        self.assertEqual(renamed.position, 2)
        self.assertEqual(renamed.title, "Новое название")
        self.assertEqual(replaced.id, target.id)
        self.assertEqual(replaced.position, 2)
        self.assertEqual(replaced.content_kind, ContentKind.AUDIO)
        self.assertEqual(replaced.content_ref, "telegram-audio-id")
        self.assertEqual(
            [item.title for item in renamed_record.lessons],
            ["Урок 1", "Новое название"],
        )
        self.assertEqual(
            [item.content_kind for item in replaced_record.lessons],
            [ContentKind.TEXT, ContentKind.AUDIO],
        )

    def test_move_uses_scratch_position_and_preserves_dense_order(self) -> None:
        _program, lessons = self._draft_with_lessons(4)

        moved_up = self.drafts.move_lesson(
            actor=self.owner_a,
            lesson_id=lessons[2].id,
            direction="up",
            now="2026-07-30T13:40:00+00:00",
        )
        self.assertEqual(
            [item.id for item in moved_up.lessons],
            [lessons[0].id, lessons[2].id, lessons[1].id, lessons[3].id],
        )
        self.assertEqual([item.position for item in moved_up.lessons], [1, 2, 3, 4])

        moved_down = self.drafts.move_lesson(
            actor=self.owner_a,
            lesson_id=lessons[2].id,
            direction="down",
            now="2026-07-30T13:41:00+00:00",
        )
        self.assertEqual([item.id for item in moved_down.lessons], [item.id for item in lessons])
        self.assertEqual([item.position for item in moved_down.lessons], [1, 2, 3, 4])

        with self.assertRaisesRegex(ProgramInvariantViolation, "up boundary"):
            self.drafts.move_lesson(
                actor=self.owner_a,
                lesson_id=lessons[0].id,
                direction="up",
            )
        with self.assertRaisesRegex(ProgramInvariantViolation, "down boundary"):
            self.drafts.move_lesson(
                actor=self.owner_a,
                lesson_id=lessons[3].id,
                direction="down",
            )
        with self.assertRaisesRegex(ValueError, "direction"):
            self.drafts.move_lesson(
                actor=self.owner_a,
                lesson_id=lessons[1].id,
                direction="sideways",
            )

    def test_archive_middle_lesson_compacts_active_positions_without_collision(self) -> None:
        program, lessons = self._draft_with_lessons(4)

        record = self.drafts.archive_lesson(
            actor=self.owner_a,
            lesson_id=lessons[1].id,
            now="2026-07-30T13:50:00+00:00",
        )
        rows = self.conn.execute(
            """
            SELECT id, position, status, archived_at
            FROM lessons
            WHERE business_id=? AND program_id=?
            ORDER BY position, id
            """,
            (self.owner_a.business_id, program.id),
        ).fetchall()
        active_rows = [row for row in rows if row["status"] == "active"]
        archived_rows = [row for row in rows if row["status"] == "archived"]

        self.assertEqual(
            [item.id for item in record.lessons],
            [lessons[0].id, lessons[2].id, lessons[3].id],
        )
        self.assertEqual([item.position for item in record.lessons], [1, 2, 3])
        self.assertEqual([row["position"] for row in active_rows], [1, 2, 3])
        self.assertEqual(len(archived_rows), 1)
        self.assertEqual(archived_rows[0]["id"], lessons[1].id)
        self.assertGreater(archived_rows[0]["position"], 3)
        self.assertEqual(
            archived_rows[0]["archived_at"],
            "2026-07-30T13:50:00+00:00",
        )
        self.assertEqual(
            len({row["position"] for row in rows}),
            len(rows),
        )
        with self.assertRaises(ProgramNotFound):
            self.drafts.archive_lesson(
                actor=self.owner_a,
                lesson_id=lessons[1].id,
            )

    def test_permissions_tenant_and_active_program_fail_closed(self) -> None:
        program, lessons = self._draft_with_lessons(2)
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
            business_id=self.owner_a.business_id,
        )
        support = self.tenancy.resolve_context(
            user_id=404,
            business_id=self.owner_a.business_id,
        )

        _record, renamed = self.drafts.update_lesson_title(
            actor=manager,
            lesson_id=lessons[0].id,
            title="Менеджер изменил",
        )
        self.assertEqual(renamed.title, "Менеджер изменил")

        for operation in (
            lambda: self.drafts.update_lesson_title(
                actor=support,
                lesson_id=lessons[0].id,
                title="Запрещено",
            ),
            lambda: self.drafts.replace_lesson_content(
                actor=support,
                lesson_id=lessons[0].id,
                content_kind="text",
                content_ref="Запрещено",
            ),
            lambda: self.drafts.archive_lesson(
                actor=support,
                lesson_id=lessons[0].id,
            ),
        ):
            with self.assertRaises(TenantPermissionDenied):
                operation()

        with self.assertRaises(ProgramNotFound):
            self.drafts.get_lesson_draft(
                actor=self.owner_b,
                lesson_id=lessons[0].id,
            )

        self.programs.publish_program(actor=self.owner_a, program_id=program.id)
        for operation in (
            lambda: self.drafts.update_lesson_title(
                actor=self.owner_a,
                lesson_id=lessons[0].id,
                title="Поздно",
            ),
            lambda: self.drafts.move_lesson(
                actor=self.owner_a,
                lesson_id=lessons[0].id,
                direction="down",
            ),
            lambda: self.drafts.archive_lesson(
                actor=self.owner_a,
                lesson_id=lessons[0].id,
            ),
        ):
            with self.assertRaises(ProgramInvariantViolation):
                operation()


if __name__ == "__main__":
    unittest.main()
