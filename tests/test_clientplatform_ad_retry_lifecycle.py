from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from clientplatform.domain.ad_connections import (
    AdConnectionNotFound,
    AdProvider,
    new_oauth_state,
    new_pkce_verifier,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.ad_connection_repository import AdConnectionRepository
from clientplatform.infrastructure.ad_credential_vault import InMemoryAdCredentialVault
from clientplatform.infrastructure.ad_worker_store import AdWorkerStore
from clientplatform.integrations.yandex_direct import YandexTokenBundle
from services.db.schema import (
    clientplatform_activity,
    clientplatform_ad_connections,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_promotions,
    clientplatform_tenancy,
)


_NOW = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)


class StaleAdvertisingLeaseRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE ad_publication_jobs(
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                available_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                locked_at TEXT,
                lock_token TEXT,
                last_error_code TEXT
            )
            """
        )
        rows = [
            (
                "stale",
                "publishing",
                "2026-08-05T09:50:00+00:00",
                "2026-08-05T09:50:00+00:00",
                "2026-08-05T09:50:00+00:00",
                "stale-lock",
                None,
            ),
            (
                "fresh",
                "publishing",
                "2026-08-05T09:58:00+00:00",
                "2026-08-05T09:58:00+00:00",
                "2026-08-05T09:58:00+00:00",
                "fresh-lock",
                None,
            ),
            (
                "queued",
                "queued",
                "2026-08-05T09:40:00+00:00",
                "2026-08-05T09:40:00+00:00",
                None,
                None,
                None,
            ),
            (
                "cancelled",
                "cancelled",
                "2026-08-05T09:40:00+00:00",
                "2026-08-05T09:40:00+00:00",
                "2026-08-05T09:40:00+00:00",
                "old-lock",
                "connection_revoked",
            ),
        ]
        self.conn.executemany(
            """
            INSERT INTO ad_publication_jobs(
                id, status, available_at, updated_at,
                locked_at, lock_token, last_error_code
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_only_expired_publishing_lease_returns_to_retry(self) -> None:
        recovered = AdWorkerStore(
            self.conn,
            vault=InMemoryAdCredentialVault(),
        ).recover_stale_publication_leases(
            lock_ttl_seconds=300,
            now=_NOW,
        )
        self.assertEqual(recovered, 1)

        stale = self.conn.execute(
            """
            SELECT status, available_at, locked_at, lock_token, last_error_code
            FROM ad_publication_jobs WHERE id='stale'
            """
        ).fetchone()
        self.assertEqual(
            tuple(stale),
            (
                "retry",
                "2026-08-05T10:00:00+00:00",
                None,
                None,
                "stale_publication_lease_recovered",
            ),
        )

        fresh = self.conn.execute(
            """
            SELECT status, locked_at, lock_token, last_error_code
            FROM ad_publication_jobs WHERE id='fresh'
            """
        ).fetchone()
        self.assertEqual(
            tuple(fresh),
            (
                "publishing",
                "2026-08-05T09:58:00+00:00",
                "fresh-lock",
                None,
            ),
        )

        queued = self.conn.execute(
            "SELECT status, locked_at, lock_token FROM ad_publication_jobs WHERE id='queued'"
        ).fetchone()
        self.assertEqual(tuple(queued), ("queued", None, None))

        cancelled = self.conn.execute(
            """
            SELECT status, locked_at, lock_token, last_error_code
            FROM ad_publication_jobs WHERE id='cancelled'
            """
        ).fetchone()
        self.assertEqual(
            tuple(cancelled),
            (
                "cancelled",
                "2026-08-05T09:40:00+00:00",
                "old-lock",
                "connection_revoked",
            ),
        )


class AdvertisingRetryLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_bookings.ensure(self.conn)
        clientplatform_promotions.ensure(self.conn)
        clientplatform_ad_connections.ensure(self.conn)
        self.vault = InMemoryAdCredentialVault()
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Сантехник")
        self.actor = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        repository = AdConnectionRepository(self.conn, vault=self.vault)
        state = new_oauth_state()
        repository.create_oauth_session(
            actor=self.actor,
            provider=AdProvider.YANDEX_DIRECT,
            state=state,
            verifier=new_pkce_verifier(),
            now=_NOW,
        )
        session, _verifier = repository.consume_oauth_session(state=state, now=_NOW)
        self.connection = repository.activate_oauth_connection(
            session=session,
            external_account_id="100500",
            external_login="vasya",
            token_bundle_json=YandexTokenBundle(
                access_token="access-one",
                token_type="bearer",
                expires_in=3600,
                refresh_token="refresh-one",
                scope=("direct:api",),
            ).to_json(),
            permissions=("campaigns.read", "adgroups.write", "ads.write"),
            now=_NOW,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_attention_connection_can_retry_and_refresh_back_to_active(self) -> None:
        self.conn.execute(
            "UPDATE ad_connections SET status='attention' WHERE id=?",
            (self.connection.id,),
        )
        worker = AdWorkerStore(self.conn, vault=self.vault)
        observed, token_json = worker.load_active(
            business_id=self.actor.business_id,
            connection_id=self.connection.id,
        )
        self.assertEqual(observed.status.value, "attention")
        self.assertEqual(
            YandexTokenBundle.from_json(token_json).access_token,
            "access-one",
        )

        worker.replace_token_bundle(
            connection=observed,
            token_bundle_json=YandexTokenBundle(
                access_token="access-two",
                token_type="bearer",
                expires_in=3600,
                refresh_token="refresh-two",
                scope=("direct:api",),
            ).to_json(),
            now="2026-08-05T10:01:00+00:00",
        )
        status, ciphertext = self.conn.execute(
            "SELECT status, credential_ciphertext FROM ad_connections WHERE id=?",
            (self.connection.id,),
        ).fetchone()
        self.assertEqual(status, "active")
        self.assertNotIn("access-two", ciphertext)
        self.assertEqual(
            YandexTokenBundle.from_json(self.vault.open(ciphertext)).access_token,
            "access-two",
        )

    def test_creative_failure_keeps_account_usable_and_preserves_safe_error(self) -> None:
        self.conn.execute(
            """
            UPDATE ad_connections
            SET status='attention', last_error_code='provider_8800'
            WHERE id=?
            """,
            (self.connection.id,),
        )
        worker = AdWorkerStore(self.conn, vault=self.vault)
        worker.keep_available_after_job_failure(
            business_id=self.actor.business_id,
            connection_id=self.connection.id,
        )
        status, error_code = self.conn.execute(
            "SELECT status, last_error_code FROM ad_connections WHERE id=?",
            (self.connection.id,),
        ).fetchone()
        self.assertEqual((status, error_code), ("active", "provider_8800"))

    def test_disabled_and_revoked_connections_remain_blocked(self) -> None:
        worker = AdWorkerStore(self.conn, vault=self.vault)
        for status in ("disabled", "revoked"):
            with self.subTest(status=status):
                self.conn.execute(
                    "UPDATE ad_connections SET status=? WHERE id=?",
                    (status, self.connection.id),
                )
                worker.keep_available_after_job_failure(
                    business_id=self.actor.business_id,
                    connection_id=self.connection.id,
                )
                stored_status = self.conn.execute(
                    "SELECT status FROM ad_connections WHERE id=?",
                    (self.connection.id,),
                ).fetchone()[0]
                self.assertEqual(stored_status, status)
                with self.assertRaises(AdConnectionNotFound):
                    worker.load_active(
                        business_id=self.actor.business_id,
                        connection_id=self.connection.id,
                    )


if __name__ == "__main__":
    unittest.main()
