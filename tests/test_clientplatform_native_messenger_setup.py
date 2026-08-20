from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from clientplatform.infrastructure.native_messenger_setup_repository import (
    NativeMessengerSetupRejected,
    NativeMessengerSetupRepository,
)
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_connections,
    clientplatform_messenger_channels,
    clientplatform_tenancy,
)


class NativeMessengerSetupRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        clientplatform_messenger_channels.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        business = tenancy.create_business(owner_user_id=101, name="Практика")
        self.actor = tenancy.resolve_context(
            user_id=101,
            business_id=business.business.id,
        )
        self.repo = NativeMessengerSetupRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_setup_token_is_digest_only_and_single_use(self) -> None:
        issued = self.repo.issue(
            actor=self.actor,
            platform="vk",
            ttl_seconds=600,
            now=datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc),
        )
        row = self.conn.execute(
            "SELECT token_digest FROM messenger_connection_setup_sessions"
        ).fetchone()
        self.assertEqual(len(str(row["token_digest"])), 64)
        self.assertNotEqual(issued.token, row["token_digest"])
        grant = self.repo.consume(
            token=issued.token,
            now=datetime(2026, 8, 21, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(grant.business_id, self.actor.business_id)
        self.assertEqual(grant.platform.value, "vk")
        with self.assertRaises(NativeMessengerSetupRejected):
            self.repo.consume(
                token=issued.token,
                now=datetime(2026, 8, 21, 0, 2, tzinfo=timezone.utc),
            )

    def test_new_setup_link_invalidates_previous_for_same_platform(self) -> None:
        first = self.repo.issue(actor=self.actor, platform="max")
        second = self.repo.issue(actor=self.actor, platform="max")
        with self.assertRaises(NativeMessengerSetupRejected):
            self.repo.inspect(token=first.token)
        self.assertEqual(self.repo.inspect(token=second.token).platform.value, "max")


if __name__ == "__main__":
    unittest.main()
