from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from clientplatform.domain.ad_connections import (
    AdConnectionInvariantViolation,
    AdProvider,
    new_oauth_state,
    new_pkce_verifier,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.ad_connection_repository import AdConnectionRepository
from clientplatform.infrastructure.ad_credential_vault import InMemoryAdCredentialVault
from clientplatform.infrastructure.ad_oauth_session_store import AdOAuthSessionStore
from services.db.schema import clientplatform_ad_connections, clientplatform_tenancy


_NOW = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)


class AdOAuthSessionCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_ad_connections.ensure(self.conn)
        self.vault = InMemoryAdCredentialVault()
        tenancy = TenancyRepository(self.conn)
        first = tenancy.create_business(owner_user_id=101, name="Первый бизнес")
        second = tenancy.create_business(owner_user_id=202, name="Второй бизнес")
        self.first_actor = tenancy.resolve_context(
            user_id=101,
            business_id=first.business.id,
        )
        self.second_actor = tenancy.resolve_context(
            user_id=202,
            business_id=second.business.id,
        )
        self.repository = AdConnectionRepository(self.conn, vault=self.vault)
        self.store = AdOAuthSessionStore(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _create_session(self) -> str:
        state = new_oauth_state()
        self.repository.create_oauth_session(
            actor=self.first_actor,
            provider=AdProvider.YANDEX_DIRECT,
            state=state,
            verifier=new_pkce_verifier(),
            now=_NOW,
        )
        return state

    def test_cancel_is_one_time_audited_and_blocks_completion(self) -> None:
        state = self._create_session()

        self.assertTrue(
            self.store.cancel(
                actor=self.first_actor,
                provider=AdProvider.YANDEX_DIRECT,
                state=state,
                now=_NOW,
            )
        )
        self.assertFalse(
            self.store.cancel(
                actor=self.first_actor,
                provider=AdProvider.YANDEX_DIRECT,
                state=state,
                now=_NOW,
            )
        )
        row = self.conn.execute(
            "SELECT consumed_at FROM ad_oauth_sessions"
        ).fetchone()
        self.assertIsNotNone(row["consumed_at"])
        audit_rows = self.conn.execute(
            """
            SELECT details_json FROM ad_audit_events
            WHERE action='ad_oauth_cancelled'
            """
        ).fetchall()
        self.assertEqual(len(audit_rows), 1)
        self.assertEqual(
            json.loads(audit_rows[0]["details_json"]),
            {"reason": "owner_cancelled"},
        )
        self.assertNotIn(state, audit_rows[0]["details_json"])
        with self.assertRaisesRegex(
            AdConnectionInvariantViolation,
            "invalid, expired or already used",
        ):
            self.repository.consume_oauth_session(state=state, now=_NOW)

    def test_other_business_cannot_cancel_session(self) -> None:
        state = self._create_session()

        self.assertFalse(
            self.store.cancel(
                actor=self.second_actor,
                provider=AdProvider.YANDEX_DIRECT,
                state=state,
                now=_NOW,
            )
        )
        session, verifier = self.repository.consume_oauth_session(
            state=state,
            now=_NOW,
        )
        self.assertEqual(session.business_id, self.first_actor.business_id)
        self.assertTrue(verifier)
        audit_count = self.conn.execute(
            "SELECT COUNT(*) FROM ad_audit_events WHERE action='ad_oauth_cancelled'"
        ).fetchone()[0]
        self.assertEqual(audit_count, 0)


if __name__ == "__main__":
    unittest.main()
