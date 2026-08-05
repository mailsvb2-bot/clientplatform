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

    def test_disabled_and_revoked_connections_remain_blocked(self) -> None:
        worker = AdWorkerStore(self.conn, vault=self.vault)
        for status in ("disabled", "revoked"):
            with self.subTest(status=status):
                self.conn.execute(
                    "UPDATE ad_connections SET status=? WHERE id=?",
                    (status, self.connection.id),
                )
                with self.assertRaises(AdConnectionNotFound):
                    worker.load_active(
                        business_id=self.actor.business_id,
                        connection_id=self.connection.id,
                    )


if __name__ == "__main__":
    unittest.main()
