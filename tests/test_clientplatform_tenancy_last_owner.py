from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.tenancy import PlatformRole, TenantInvariantViolation
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import clientplatform_tenancy


class ClientPlatformLastOwnerInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        self.repository = TenancyRepository(self.conn)
        access = self.repository.create_business(
            owner_user_id=101,
            name="Owner invariant",
            now="2026-08-09T18:00:00+00:00",
        )
        self.business_id = access.business.id
        self.owner = self.repository.resolve_context(
            user_id=101,
            business_id=self.business_id,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_last_owner_cannot_be_demoted_through_grant_member(self) -> None:
        with self.assertRaisesRegex(
            TenantInvariantViolation,
            "retain at least one active owner",
        ):
            self.repository.grant_member(
                actor=self.owner,
                user_id=101,
                role=PlatformRole.ADMINISTRATOR,
                now="2026-08-09T18:01:00+00:00",
            )

        current = self.repository.resolve_context(
            user_id=101,
            business_id=self.business_id,
        )
        self.assertEqual(current.role, PlatformRole.OWNER)

    def test_owner_can_be_demoted_after_second_owner_is_active(self) -> None:
        self.repository.grant_member(
            actor=self.owner,
            user_id=202,
            role=PlatformRole.OWNER,
            now="2026-08-09T18:01:00+00:00",
        )
        updated = self.repository.grant_member(
            actor=self.owner,
            user_id=101,
            role=PlatformRole.ADMINISTRATOR,
            now="2026-08-09T18:02:00+00:00",
        )

        self.assertEqual(updated.role, PlatformRole.ADMINISTRATOR)
        remaining_owner = self.repository.resolve_context(
            user_id=202,
            business_id=self.business_id,
        )
        self.assertEqual(remaining_owner.role, PlatformRole.OWNER)

    def test_last_owner_revoke_uses_same_serialized_invariant(self) -> None:
        with self.assertRaisesRegex(
            TenantInvariantViolation,
            "retain at least one active owner",
        ):
            self.repository.revoke_member(
                actor=self.owner,
                user_id=101,
                now="2026-08-09T18:03:00+00:00",
            )

        current = self.repository.resolve_context(
            user_id=101,
            business_id=self.business_id,
        )
        self.assertEqual(current.role, PlatformRole.OWNER)


if __name__ == "__main__":
    unittest.main()
