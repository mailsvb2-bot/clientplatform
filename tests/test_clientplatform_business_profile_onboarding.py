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

    def test_review_contains_only_non_empty_owner_facts(self) -> None:
        lines = business_profile_review_lines(
            BusinessProfileDetails(prices=("2500 ₽",), geo=("Казань",))
        )
        self.assertEqual(lines, ("Цены: 2500 ₽", "География: Казань"))

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
