from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.business_profile import (
    BusinessProfileDetails,
    business_profile_details_from_json,
    business_profile_details_to_json,
    business_profile_review_lines,
    extract_explicit_business_profile_details,
)
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.business_profile_details_repository import (
    BusinessProfileDetailsRepository,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import clientplatform_activity, clientplatform_tenancy


def _memory_repositories():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    clientplatform_tenancy.ensure(conn)
    clientplatform_activity.ensure(conn)
    return conn, TenancyRepository(conn), ActivityRepository(conn), BusinessProfileDetailsRepository(conn)


class ClientPlatformBusinessProfileOnboardingTests(unittest.TestCase):
    def test_explicit_profile_extraction_never_invents_missing_fields(self) -> None:
        details = extract_explicit_business_profile_details(
            "Психолог, работаю онлайн. Цена 5000 ₽. "
            "Город: Москва; Аудитория: взрослые с тревогой; "
            "Контакт: @maria_help; https://example.test/about"
        )

        self.assertEqual(details.prices, ("5000 ₽",))
        self.assertEqual(details.geo, ("Москва",))
        self.assertEqual(details.audiences, ("взрослые с тревогой",))
        self.assertEqual(details.contacts, ("@maria_help",))
        self.assertEqual(details.source_urls, ("https://example.test/about",))
        self.assertEqual(details.services, ())
        self.assertEqual(details.legal_constraints, ())
        self.assertIsNone(details.preferred_conversion_action)

    def test_explicit_profile_extraction_covers_owner_labeled_surface(self) -> None:
        details = extract_explicit_business_profile_details(
            "Услуги: консультация, разбор; Продукты: курс; Для кого: родители; "
            "География: онлайн; Часы работы: пн-пт 10:00-18:00; "
            "Правила записи: по предоплате; Стиль общения: спокойно; "
            "Можно утверждать: индивидуальный подход; "
            "Нельзя утверждать: гарантия результата; "
            "Правовые ограничения: без медицинских обещаний; "
            "Визуальные материалы: логотип; FAQ: запись, перенос; "
            "Условия продажи: возврат до начала; "
            "Главное действие клиента: записаться; "
            "Цена 3 500 руб.; mail@example.test; +7 (999) 123-45-67; @owner_help; "
            "https://example.test/source"
        )

        self.assertEqual(details.services, ("консультация", "разбор"))
        self.assertEqual(details.products, ("курс",))
        self.assertEqual(details.audiences, ("родители",))
        self.assertEqual(details.geo, ("онлайн",))
        self.assertEqual(details.working_hours, "пн-пт 10:00-18:00")
        self.assertEqual(details.booking_rules, "по предоплате")
        self.assertEqual(details.tone_of_voice, "спокойно")
        self.assertEqual(details.allowed_claims, ("индивидуальный подход",))
        self.assertEqual(details.prohibited_claims, ("гарантия результата",))
        self.assertEqual(details.legal_constraints, ("без медицинских обещаний",))
        self.assertEqual(details.visual_assets, ("логотип",))
        self.assertEqual(details.faq, ("запись", "перенос"))
        self.assertEqual(details.sales_terms, "возврат до начала")
        self.assertEqual(details.preferred_conversion_action, "записаться")
        self.assertEqual(details.prices, ("3 500 руб.",))
        self.assertEqual(
            details.contacts,
            ("mail@example.test", "+7 (999) 123-45-67", "@owner_help"),
        )
        self.assertEqual(details.source_urls, ("https://example.test/source",))

    def test_profile_details_json_round_trip_is_stable_and_strict(self) -> None:
        details = BusinessProfileDetails(
            services=("Консультация",),
            prices=("5 000 ₽",),
            tone_of_voice="спокойный и профессиональный",
            prohibited_claims=("гарантия результата",),
        )
        encoded = business_profile_details_to_json(details)

        self.assertEqual(business_profile_details_from_json(encoded), details)
        self.assertEqual(
            business_profile_details_to_json(business_profile_details_from_json(encoded)),
            encoded,
        )
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            business_profile_details_from_json('{"secret_memory":"no"}')

    def test_profile_details_validation_fails_closed(self) -> None:
        self.assertEqual(BusinessProfileDetails.from_payload(None), BusinessProfileDetails())
        with self.assertRaisesRegex(ValueError, "must be an object"):
            BusinessProfileDetails.from_payload("not-an-object")
        with self.assertRaisesRegex(ValueError, "stored business profile details are invalid"):
            business_profile_details_from_json("{")
        with self.assertRaisesRegex(ValueError, "details must be BusinessProfileDetails"):
            business_profile_details_to_json({})  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "list field must be a list of strings"):
            BusinessProfileDetails(services=123)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "at most 32 items"):
            BusinessProfileDetails(services=tuple(f"service-{index}" for index in range(33)))
        with self.assertRaisesRegex(ValueError, "at most 2000 characters"):
            BusinessProfileDetails(working_hours="x" * 2001)

    def test_profile_details_normalize_deduplicate_and_ignore_empty_values(self) -> None:
        details = BusinessProfileDetails(
            services=("  Консультация  ", "консультация", "", "\x00"),
            contacts=" @owner_help ",
            working_hours="  пн-пт   10:00-18:00  ",
        )
        self.assertEqual(details.services, ("Консультация",))
        self.assertEqual(details.contacts, ("@owner_help",))
        self.assertEqual(details.working_hours, "пн-пт 10:00-18:00")

    def test_review_contains_only_non_empty_owner_facts(self) -> None:
        lines = business_profile_review_lines(
            BusinessProfileDetails(prices=("2500 ₽",), geo=("Казань",))
        )
        self.assertEqual(lines, ("Цены: 2500 ₽", "География: Казань"))

    def test_review_renders_full_supported_human_surface(self) -> None:
        lines = business_profile_review_lines(
            BusinessProfileDetails(
                services=("Консультация",),
                products=("Курс",),
                audiences=("Родители",),
                contacts=("@owner",),
                working_hours="пн-пт",
                booking_rules="по записи",
                tone_of_voice="спокойно",
                preferred_conversion_action="записаться",
                source_urls=("https://example.test",),
            )
        )
        self.assertIn("Услуги: Консультация", lines)
        self.assertIn("Продукты: Курс", lines)
        self.assertIn("Для кого: Родители", lines)
        self.assertIn("Контакты: @owner", lines)
        self.assertIn("Часы работы: пн-пт", lines)
        self.assertIn("Правила записи: по записи", lines)
        self.assertIn("Стиль общения: спокойно", lines)
        self.assertIn("Главное действие клиента: записаться", lines)
        self.assertIn("Источники: https://example.test", lines)

    def test_profile_details_are_durable_confirmed_and_tenant_scoped(self) -> None:
        conn, tenancy, activity, details_repo = _memory_repositories()
        try:
            access_a = tenancy.create_business(owner_user_id=101, name="Практика А")
            access_b = tenancy.create_business(owner_user_id=202, name="Практика Б")
            actor_a = tenancy.resolve_context(user_id=101, business_id=access_a.business.id)
            actor_b = tenancy.resolve_context(user_id=202, business_id=access_b.business.id)
            activity.upsert_profile(
                actor=actor_a,
                activity_description="Консультации для родителей",
                timezone_name="Europe/Moscow",
            )
            activity.upsert_profile(
                actor=actor_b,
                activity_description="Ремонт автомобилей",
                timezone_name="Europe/Moscow",
            )

            saved_a = details_repo.save(
                actor=actor_a,
                details=BusinessProfileDetails(prices=("5000 ₽",), geo=("Москва",)),
                now="2026-08-19T00:00:00+00:00",
            )
            saved_b = details_repo.save(
                actor=actor_b,
                details=BusinessProfileDetails(prices=("9000 ₽",), geo=("Казань",)),
                now="2026-08-19T00:00:01+00:00",
            )
            self.assertFalse(saved_a.confirmed)
            self.assertFalse(saved_b.confirmed)
            self.assertEqual(details_repo.get(actor=actor_a).details.prices, ("5000 ₽",))
            self.assertEqual(details_repo.get(actor=actor_b).details.prices, ("9000 ₽",))

            confirmed = details_repo.confirm(
                actor=actor_a,
                now="2026-08-19T00:01:00+00:00",
            )
            self.assertEqual(confirmed.confirmed_at, "2026-08-19T00:01:00+00:00")
            self.assertFalse(details_repo.get(actor=actor_b).confirmed)

            repeated = details_repo.confirm(
                actor=actor_a,
                now="2026-08-19T00:02:00+00:00",
            )
            self.assertEqual(repeated.confirmed_at, confirmed.confirmed_at)

            preserved = details_repo.save(
                actor=actor_a,
                details=BusinessProfileDetails(prices=("5500 ₽",), geo=("Москва",)),
                reset_confirmation=False,
                now="2026-08-19T00:03:00+00:00",
            )
            self.assertTrue(preserved.confirmed)
            self.assertEqual(preserved.details.prices, ("5500 ₽",))
        finally:
            conn.close()

    def test_schema_additive_migration_keeps_existing_profile_data(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(conn)
        conn.execute(
            """
            CREATE TABLE business_profiles(
                business_id TEXT PRIMARY KEY,
                activity_description TEXT NOT NULL,
                timezone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_by_member_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        clientplatform_activity.ensure(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(business_profiles)")}
        self.assertIn("profile_details_json", columns)
        self.assertIn("profile_confirmed_at", columns)
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
