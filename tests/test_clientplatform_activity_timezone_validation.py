from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.activity import normalize_known_timezone, normalize_timezone
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import clientplatform_activity, clientplatform_tenancy


class ClientPlatformActivityTimezoneValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.actor = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.repo = ActivityRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_known_iana_timezone_is_persisted(self) -> None:
        profile = self.repo.upsert_profile(
            actor=self.actor,
            activity_description="Консультации",
            timezone_name="Europe/Amsterdam",
            now="2026-08-08T00:00:00+00:00",
        )
        self.assertEqual(profile.timezone, "Europe/Amsterdam")

    def test_unknown_timezone_is_rejected_before_profile_write(self) -> None:
        with self.assertRaisesRegex(ValueError, "known IANA timezone"):
            self.repo.upsert_profile(
                actor=self.actor,
                activity_description="Консультации",
                timezone_name="Mars/Olympus",
                now="2026-08-08T00:00:00+00:00",
            )

        count = self.conn.execute(
            "SELECT COUNT(*) FROM business_profiles"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_legacy_normalizer_remains_non_resolving_for_read_recovery(self) -> None:
        self.assertEqual(normalize_timezone("Mars/Olympus"), "Mars/Olympus")
        with self.assertRaisesRegex(ValueError, "known IANA timezone"):
            normalize_known_timezone("Mars/Olympus")


if __name__ == "__main__":
    unittest.main()
