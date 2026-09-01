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

    def test_session_is_durable_channel_scoped_and_tenant_safe(self) -> None:
        saved = self.repo.set(
            user_id=101,
            platform="vk",
            business_id=self.first.business.id,
            action="price",
            context={"offering_id": "offer-1"},
            now="2026-09-01T10:00:00+00:00",
        )
        self.assertEqual(self.repo.get(user_id=101, platform="vk"), saved)
        self.assertIsNone(self.repo.get(user_id=101, platform="max"))

        with self.assertRaises(TenantAccessDenied):
            self.repo.set(
                user_id=101,
                platform="max",
                business_id=self.other.business.id,
                action="program_title",
            )

        self.repo.clear(user_id=101, platform="vk")
        self.assertIsNone(self.repo.get(user_id=101, platform="vk"))

    def test_session_is_removed_by_membership_cascade(self) -> None:
        owner = self.tenancy.resolve_context(user_id=101, business_id=self.first.business.id)
        self.tenancy.grant_member(actor=owner, user_id=303, role="manager")
        self.repo.set(
            user_id=303,
            platform="vk",
            business_id=self.first.business.id,
            action="program_title",
        )
        self.tenancy.revoke_member(actor=owner, user_id=303)
        self.assertIsNone(self.repo.get(user_id=303, platform="vk"))


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
        )
        for session, raw, action, args in cases:
            with self.subTest(action=action):
                resolved = resolve_owner_input(session, raw)
                self.assertEqual((resolved.action, resolved.args), (action, args))

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
