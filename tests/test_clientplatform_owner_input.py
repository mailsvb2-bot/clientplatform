from __future__ import annotations

import sqlite3
import unittest

from clientplatform.application.owner_input import resolve_owner_input
from clientplatform.domain.owner_input import OwnerInputSession
from clientplatform.domain.tenancy import TenantAccessDenied
from clientplatform.infrastructure.owner_input_repository import OwnerInputRepository
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db.schema import clientplatform_tenancy


class OwnerInputRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        self.repo = OwnerInputRepository(self.conn)
        self.first = self.tenancy.create_business(owner_user_id=101, name="Практика")
        self.other = self.tenancy.create_business(owner_user_id=202, name="Чужой бизнес")

    def tearDown(self) -> None:
        self.conn.close()

    def test_session_is_durable_surface_scoped_and_tenant_safe(self) -> None:
        route_surface = "route:11111111-1111-1111-1111-111111111111"
        official = self.repo.set(
            user_id=101,
            platform="vk",
            business_id=self.first.business.id,
            action="price",
            context={"offering_id": "offer-1"},
            now="2026-09-01T10:00:00+00:00",
        )
        managed = self.repo.set(
            user_id=101,
            platform="vk",
            surface=route_surface,
            business_id=self.first.business.id,
            action="program_title",
            now="2026-09-01T10:01:00+00:00",
        )
        self.assertEqual(self.repo.get(user_id=101, platform="vk"), official)
        self.assertEqual(
            self.repo.get(user_id=101, platform="vk", surface=route_surface), managed
        )
        self.assertIsNone(self.repo.get(user_id=101, platform="max"))

        event_input = self.repo.set(
            user_id=101,
            platform="max",
            business_id=self.first.business.id,
            action="online_event",
            context={"step": "legacy-compatible"},
        )
        self.assertEqual(event_input.action, "online_event")
        join_input = self.repo.set(
            user_id=101,
            platform="max",
            business_id=self.first.business.id,
            action="event_join_url",
            context={"event_id": "event-1"},
        )
        self.assertEqual(join_input.action, "event_join_url")

        with self.assertRaises(TenantAccessDenied):
            self.repo.set(
                user_id=101,
                platform="max",
                business_id=self.other.business.id,
                action="program_title",
            )

        self.repo.clear(user_id=101, platform="vk", surface=route_surface)
        self.assertIsNone(
            self.repo.get(user_id=101, platform="vk", surface=route_surface)
        )
        self.assertEqual(self.repo.get(user_id=101, platform="vk"), official)

    def test_direction_actions_start_durable_vk_and_max_sessions(self) -> None:
        direction_id = "11111111-1111-4111-8111-111111111111"
        created = self.repo.set(
            user_id=101,
            platform="vk",
            business_id=self.first.business.id,
            action="activity_direction_create",
            now="2026-09-22T00:00:00+00:00",
        )
        edited = self.repo.set(
            user_id=101,
            platform="max",
            business_id=self.first.business.id,
            action="activity_direction_edit",
            context={"direction_id": direction_id},
            now="2026-09-22T00:01:00+00:00",
        )

        self.assertEqual(created.action, "activity_direction_create")
        self.assertEqual(
            self.repo.get(user_id=101, platform="vk"),
            created,
        )
        self.assertEqual(edited.action, "activity_direction_edit")
        self.assertEqual(edited.context["direction_id"], direction_id)
        self.assertEqual(
            self.repo.get(user_id=101, platform="max"),
            edited,
        )

    def test_session_is_removed_by_membership_cascade(self) -> None:
        owner = self.tenancy.resolve_context(user_id=101, business_id=self.first.business.id)
        self.tenancy.grant_member(actor=owner, user_id=303, role="manager")
        self.repo.set(
            user_id=303,
            platform="vk",
            business_id=self.first.business.id,
            action="program_title",
        )
        self.repo.set(
            user_id=303,
            platform="vk",
            surface="route:22222222-2222-2222-2222-222222222222",
            business_id=self.first.business.id,
            action="price",
            context={"offering_id": "offer-2"},
        )
        self.tenancy.revoke_member(actor=owner, user_id=303)
        self.tenancy.grant_member(actor=owner, user_id=303, role="manager")
        self.assertIsNone(self.repo.get(user_id=303, platform="vk"))
        self.assertIsNone(
            self.repo.get(
                user_id=303,
                platform="vk",
                surface="route:22222222-2222-2222-2222-222222222222",
            )
        )


class OwnerInputResolutionTests(unittest.TestCase):
    @staticmethod
    def session(action: str, **context: str) -> OwnerInputSession:
        return OwnerInputSession(
            user_id=101,
            platform="vk",
            business_id="business-1",
            action=action,
            context=context,
            updated_at="2026-09-01T10:00:00+00:00",
        )

    def test_plain_language_resolution_keeps_existing_native_actions(self) -> None:
        cases = (
            (self.session("activity_description"), "Ремонтируем автомобили", "activity-edit-text", ("Ремонтируем автомобили",)),
            (self.session("program_title"), "Первый урок", "program-create-text", ("Первый урок",)),
            (
                self.session("publication_draft", channel="vk"),
                "Новость | Открыли запись на субботу",
                "publication-new-text",
                ("vk", "Новость", "Открыли запись на субботу"),
            ),
            (
                self.session("program_lesson", program_id="program-1", content_kind="text"),
                "Введение | Добро пожаловать",
                "program-lesson-text",
                ("program-1", "text", "Введение", "Добро пожаловать"),
            ),
            (
                self.session("offering", connector_key="services"),
                "Диагностика | Проверка перед покупкой",
                "offering-new-text",
                ("services", "Диагностика", "Проверка перед покупкой"),
            ),
            (
                self.session("price", offering_id="offering-1"),
                "5000 RUB",
                "price-set-text",
                ("offering-1", "5000", "RUB"),
            ),
            (
                self.session("booking_time", offering_id="offering-1"),
                "05.09.2026 15:00 90",
                "booking-open-text",
                ("offering-1", "05.09.2026 15:00", "90"),
            ),
            (
                self.session("member_user", role_code="manager"),
                "123456",
                "member-add-text",
                ("123456", "manager"),
            ),
            (
                self.session("online_event"),
                "Вебинар | 25.09.2026 19:00 | - | -",
                "event-create-text",
                ("Вебинар", "25.09.2026 19:00", "", ""),
            ),
            (
                self.session("event_join_url", event_id="event-1"),
                "https://example.test/live",
                "event-join-text",
                ("event-1", "https://example.test/live"),
            ),
        )
        for session, raw, action, args in cases:
            with self.subTest(action=action):
                resolved = resolve_owner_input(session, raw)
                self.assertEqual((resolved.action, resolved.args), (action, args))

    def test_warmup_owner_inputs_preserve_custom_text_and_validate_days(self) -> None:
        text = resolve_owner_input(
            self.session(
                "event_warmup_text",
                event_id="event-1",
                requested_days="3",
                position="2",
            ),
            "Мой текст\n\nС новой строки",
        )
        self.assertEqual(
            (text.action, text.args),
            (
                "event-warmup-edit-text",
                ("event-1", "3", "2", "Мой текст\n\nС новой строки"),
            ),
        )

        days = resolve_owner_input(
            self.session("event_warmup_days", event_id="event-1", maximum="10"),
            "6",
        )
        self.assertEqual(
            (days.action, days.args),
            ("event-warmup-days-text", ("event-1", "6")),
        )
        with self.assertRaises(ValueError):
            resolve_owner_input(
                self.session("event_warmup_days", event_id="event-1", maximum="5"),
                "6",
            )

    def test_followup_owner_input_preserves_custom_text(self) -> None:
        resolved = resolve_owner_input(
            self.session(
                "event_followup_text",
                event_id="event-1",
                index="4",
                segment="attended_unpaid",
                stage="2",
            ),
            "Мой дожим\n\n{name}, детали здесь: {offer}",
        )
        self.assertEqual(
            (resolved.action, resolved.args),
            (
                "event-followup-edit-text",
                (
                    "event-1",
                    "4",
                    "attended_unpaid",
                    "2",
                    "Мой дожим\n\n{name}, детали здесь: {offer}",
                ),
            ),
        )
        with self.assertRaises(ValueError):
            resolve_owner_input(
                self.session(
                    "event_followup_text",
                    event_id="event-1",
                    index="4",
                    segment="attended_unpaid",
                    stage="",
                ),
                "Текст",
            )

    def test_webinar_wizard_input_resolution_is_step_scoped(self) -> None:
        cases = (
            ("title", "Большой интенсив", "event-wizard-title-text", ("Большой интенсив",)),
            ("count", "3", "event-wizard-count-text", ("3",)),
            ("timezone", "Europe/Amsterdam", "event-wizard-timezone-text", ("Europe/Amsterdam",)),
            ("manual_window", "25.09.2026 19:00-21:00", "event-wizard-window-text", ("25.09.2026 19:00-21:00",)),
            ("room", "https://example.test/day-1", "event-wizard-room-text", ("https://example.test/day-1",)),
            ("warmup_days", "5", "event-wizard-warmup-text", ("5",)),
        )
        for step, raw, action, args in cases:
            with self.subTest(step=step):
                resolved = resolve_owner_input(self.session("online_event", step=step), raw)
                self.assertEqual((resolved.action, resolved.args), (action, args))

        topics = resolve_owner_input(
            self.session("online_event", step="topics", count="3"),
            "Диагностика\nПрактика\nПлан действий",
        )
        self.assertEqual(
            (topics.action, topics.args),
            (
                "event-wizard-topics-text",
                ("Диагностика\nПрактика\nПлан действий",),
            ),
        )
        with self.assertRaises(ValueError):
            resolve_owner_input(
                self.session("online_event", step="topics", count="3"),
                "Только одна тема",
            )

        with self.assertRaises(ValueError):
            resolve_owner_input(self.session("online_event", step="count"), "32")

    def test_free_text_fields_preserve_multiline_formatting(self) -> None:
        activity = resolve_owner_input(
            self.session("activity_description"),
            "Первая строка\n\nВторая  строка",
        )
        self.assertEqual(activity.args, ("Первая строка\n\nВторая  строка",))

        publication = resolve_owner_input(
            self.session("publication_draft", channel="vk"),
            "Заголовок | первая строка\nвторая  строка",
        )
        self.assertEqual(
            publication.args,
            ("vk", "Заголовок", "первая строка\nвторая  строка"),
        )

        lesson = resolve_owner_input(
            self.session(
                "program_lesson", program_id="program-1", content_kind="text"
            ),
            "Введение | абзац один\n\nабзац два",
        )
        self.assertEqual(
            lesson.args,
            ("program-1", "text", "Введение", "абзац один\n\nабзац два"),
        )

    def test_payment_accepts_simple_and_legacy_advanced_forms(self) -> None:
        session = self.session("payment")
        simple = resolve_owner_input(session, "3500 RUB | консультация")
        self.assertEqual(
            (simple.action, simple.args),
            ("payment-new-text", ("3500", "RUB", "-", "-", "консультация")),
        )
        legacy = resolve_owner_input(
            session,
            "оплата 3500 RUB abcdef12 fedcba98 | консультация",
        )
        self.assertEqual(
            (legacy.action, legacy.args),
            (
                "payment-new-text",
                ("3500", "RUB", "abcdef12", "fedcba98", "консультация"),
            ),
        )

    def test_invalid_plain_answer_fails_without_guessing(self) -> None:
        with self.assertRaises(ValueError):
            resolve_owner_input(self.session("price", offering_id="x"), "пять тысяч")


if __name__ == "__main__":
    unittest.main()
