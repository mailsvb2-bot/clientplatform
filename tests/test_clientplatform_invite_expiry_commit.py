from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from clientplatform.application import activity as activity_application
from clientplatform.domain.activity import ActivityInvariantViolation
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.postgres_safe_activity_repository import ActivityRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_customers,
    clientplatform_tenancy,
)


class ClientPlatformInviteExpiryCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)

        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.issued = ActivityRepository(self.conn).issue_customer_invite(
            actor=self.owner,
            ttl_days=1,
            now="2026-01-01T00:00:00+00:00",
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _get_db(self):
        conn = self.conn

        @contextmanager
        def transaction():
            try:
                yield conn
            except ActivityInvariantViolation:
                conn.rollback()
                raise
            else:
                conn.commit()

        return transaction

    def test_expired_invite_state_commits_before_user_facing_error(self) -> None:
        get_db = self._get_db()
        with (
            patch.object(activity_application, "get_db", get_db),
            patch(
                "clientplatform.infrastructure.activity_repository._utc_now",
                return_value="2026-01-03T00:00:00+00:00",
            ),
        ):
            with self.assertRaisesRegex(
                ActivityInvariantViolation,
                "Срок действия ссылки истёк",
            ):
                activity_application.claim_customer_invite(
                    token=self.issued.token,
                    telegram_user_id=700001,
                    username="customer",
                    display_name="Клиент",
                )

        row = self.conn.execute(
            "SELECT status, claimed_customer_id FROM customer_invites WHERE id=?",
            (self.issued.invite.id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"]), "expired")
        self.assertIsNone(row["claimed_customer_id"])

        customer_count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM customers"
        ).fetchone()["c"]
        self.assertEqual(int(customer_count), 0)


if __name__ == "__main__":
    unittest.main()
