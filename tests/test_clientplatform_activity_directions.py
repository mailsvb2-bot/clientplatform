from __future__ import annotations

import sqlite3
import unittest
from uuid import uuid4

from clientplatform.domain.activity_directions import (
    ActivityDirectionInvariantViolation,
    ActivityDirectionNotFound,
    ActivityDirectionStatus,
    DirectionSubjectKind,
)
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.infrastructure.activity_direction_repository import (
    ActivityDirectionRepository,
)
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.program_repository import ProgramRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_activity_directions,
    clientplatform_connections,
    clientplatform_events,
    clientplatform_programs,
    clientplatform_tenancy,
)


class ActivityDirectionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_activity_directions.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_programs.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        clientplatform_events.ensure(self.conn)

        tenancy = TenancyRepository(self.conn)
        self.access_a = tenancy.create_business(
            owner_user_id=61001,
            name="Организация А",
            now="2026-09-21T12:00:00+00:00",
        )
        self.access_b = tenancy.create_business(
            owner_user_id=62001,
            name="Организация Б",
            now="2026-09-21T12:00:00+00:00",
        )
        self.owner_a = tenancy.resolve_context(
            user_id=61001,
            business_id=self.access_a.business.id,
        )
        self.owner_b = tenancy.resolve_context(
            user_id=62001,
            business_id=self.access_b.business.id,
        )
        self.repo = ActivityDirectionRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_one_organization_can_have_multiple_independent_directions(self) -> None:
        first = self.repo.create(
            actor=self.owner_a,
            title="Работа с частными клиентами",
            description="Отдельная линейка предложений для частных клиентов.",
            now="2026-09-21T12:01:00+00:00",
        )
        second = self.repo.create(
            actor=self.owner_a,
            title="Работа с организациями",
            description="Проекты и услуги для компаний и команд.",
            now="2026-09-21T12:02:00+00:00",
        )
        listed = self.repo.list(actor=self.owner_a)
        self.assertEqual([item.id for item in listed], [first.id, second.id])
        self.assertEqual({item.business_id for item in listed}, {self.owner_a.business_id})

    def test_direction_is_tenant_isolated_and_owner_admin_managed(self) -> None:
        direction = self.repo.create(
            actor=self.owner_a,
            title="Основное направление",
            description="Описание направления.",
        )
        with self.assertRaises(ActivityDirectionNotFound):
            self.repo.get(actor=self.owner_b, direction_id=direction.id)

        tenancy = TenancyRepository(self.conn)
        tenancy.grant_member(actor=self.owner_a, user_id=61002, role="manager")
        manager = tenancy.resolve_context(
            user_id=61002,
            business_id=self.owner_a.business_id,
        )
        with self.assertRaises(TenantPermissionDenied):
            self.repo.create(
                actor=manager,
                title="Недопустимое направление",
                description="Менеджер не меняет структуру организации.",
            )

    def test_content_manager_can_place_program_in_existing_direction(self) -> None:
        direction = self.repo.create(
            actor=self.owner_a,
            title="Контентное направление",
            description="Структуру создал владелец.",
        )
        tenancy = TenancyRepository(self.conn)
        tenancy.grant_member(
            actor=self.owner_a,
            user_id=61003,
            role="content_manager",
        )
        content_manager = tenancy.resolve_context(
            user_id=61003,
            business_id=self.owner_a.business_id,
        )
        program = ProgramRepository(self.conn).create_program(
            actor=content_manager,
            title="Материал направления",
        )
        binding = self.repo.bind_subject(
            actor=content_manager,
            direction_id=direction.id,
            subject_kind="program",
            subject_id=program.id,
        )
        self.assertEqual(binding.direction_id, direction.id)
        self.assertEqual(binding.subject_id, program.id)

    def test_archive_preserves_history_and_restore_returns_same_direction(self) -> None:
        direction = self.repo.create(
            actor=self.owner_a,
            title="Временное направление",
            description="Направление можно убрать из активной работы.",
            now="2026-09-21T12:01:00+00:00",
        )
        archived = self.repo.archive(
            actor=self.owner_a,
            direction_id=direction.id,
            now="2026-09-21T12:02:00+00:00",
        )
        self.assertEqual(archived.status, ActivityDirectionStatus.ARCHIVED)
        self.assertEqual(self.repo.list(actor=self.owner_a), [])
        self.assertEqual(
            [item.id for item in self.repo.list(actor=self.owner_a, include_archived=True)],
            [direction.id],
        )
        restored = self.repo.restore(
            actor=self.owner_a,
            direction_id=direction.id,
            now="2026-09-21T12:03:00+00:00",
        )
        self.assertEqual(restored.id, direction.id)
        self.assertEqual(restored.status, ActivityDirectionStatus.ACTIVE)

    def test_direction_groups_program_offering_and_event_without_copying_them(self) -> None:
        direction = self.repo.create(
            actor=self.owner_a,
            title="Направление А",
            description="Общая группа разных форматов работы.",
        )
        program = ProgramRepository(self.conn).create_program(
            actor=self.owner_a,
            title="Материалы направления",
        )
        activity = ActivityRepository(self.conn)
        activity.upsert_profile(
            actor=self.owner_a,
            activity_description="Организация оказывает несколько видов услуг.",
            timezone_name="Europe/Moscow",
        )
        capability = activity.enable_capability(
            actor=self.owner_a,
            connector_key="services",
        )
        offering = activity.create_offering(
            actor=self.owner_a,
            capability_id=capability.id,
            title="Услуга направления",
            description="Описание услуги.",
        )
        event_id = str(uuid4())
        self.conn.execute(
            """
            INSERT INTO clientplatform_events(
                id,business_id,created_by_member_id,kind,status,title,description,
                starts_at,ends_at,timezone_name,provider_key,provider_label,
                join_url,offer_url,public_slug,consent_version,
                notification_connection_id,created_at,updated_at
            ) VALUES(?,?,?,'online_event','draft',?,'',
                     '2026-10-01T10:00:00+00:00',NULL,'Europe/Moscow',
                     'manual',NULL,'https://example.invalid/join',NULL,?,
                     'v1',NULL,'2026-09-21T12:00:00+00:00',
                     '2026-09-21T12:00:00+00:00')
            """,
            (
                event_id,
                self.owner_a.business_id,
                self.owner_a.membership_id,
                "Событие направления",
                "direction-" + uuid4().hex,
            ),
        )

        bindings = [
            self.repo.bind_subject(
                actor=self.owner_a,
                direction_id=direction.id,
                subject_kind=DirectionSubjectKind.PROGRAM,
                subject_id=program.id,
            ),
            self.repo.bind_subject(
                actor=self.owner_a,
                direction_id=direction.id,
                subject_kind=DirectionSubjectKind.OFFERING,
                subject_id=offering.id,
            ),
            self.repo.bind_subject(
                actor=self.owner_a,
                direction_id=direction.id,
                subject_kind=DirectionSubjectKind.EVENT,
                subject_id=event_id,
            ),
        ]
        self.assertEqual(
            {item.subject_kind for item in bindings},
            {
                DirectionSubjectKind.PROGRAM,
                DirectionSubjectKind.OFFERING,
                DirectionSubjectKind.EVENT,
            },
        )
        stored = self.repo.list_bindings(actor=self.owner_a, direction_id=direction.id)
        self.assertEqual(len(stored), 3)

    def test_subject_can_move_between_directions_but_not_between_organizations(self) -> None:
        first = self.repo.create(
            actor=self.owner_a,
            title="Первое",
            description="Первое направление.",
        )
        second = self.repo.create(
            actor=self.owner_a,
            title="Второе",
            description="Второе направление.",
        )
        program = ProgramRepository(self.conn).create_program(
            actor=self.owner_a,
            title="Объект",
        )
        self.repo.bind_subject(
            actor=self.owner_a,
            direction_id=first.id,
            subject_kind="program",
            subject_id=program.id,
        )
        moved = self.repo.bind_subject(
            actor=self.owner_a,
            direction_id=second.id,
            subject_kind="program",
            subject_id=program.id,
        )
        self.assertEqual(moved.direction_id, second.id)
        self.assertEqual(self.repo.list_bindings(actor=self.owner_a, direction_id=first.id), [])

        foreign_program = ProgramRepository(self.conn).create_program(
            actor=self.owner_b,
            title="Чужой объект",
        )
        with self.assertRaises(ActivityDirectionNotFound):
            self.repo.bind_subject(
                actor=self.owner_a,
                direction_id=second.id,
                subject_kind="program",
                subject_id=foreign_program.id,
            )

    def test_archived_direction_rejects_new_binding(self) -> None:
        direction = self.repo.create(
            actor=self.owner_a,
            title="Архив",
            description="Будет архивировано.",
        )
        program = ProgramRepository(self.conn).create_program(
            actor=self.owner_a,
            title="Материал",
        )
        self.repo.archive(actor=self.owner_a, direction_id=direction.id)
        with self.assertRaises(ActivityDirectionInvariantViolation):
            self.repo.bind_subject(
                actor=self.owner_a,
                direction_id=direction.id,
                subject_kind="program",
                subject_id=program.id,
            )


if __name__ == "__main__":
    unittest.main()
