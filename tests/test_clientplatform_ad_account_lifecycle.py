from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

from clientplatform.domain.ad_connections import (
    AdConnectionStatus,
    AdProvider,
    new_oauth_state,
    new_pkce_verifier,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.ad_connection_repository import AdConnectionRepository
from clientplatform.infrastructure.ad_credential_vault import (
    AdCredentialVaultError,
    AgeAdCredentialVault,
    InMemoryAdCredentialVault,
)
from clientplatform.infrastructure.ad_worker_store import (
    AdConnectionLifecycleStore,
    AdWorkerStore,
)
from clientplatform.integrations.yandex_direct import YandexTokenBundle
from clientplatform.integrations.yandex_oauth_lifecycle import YandexOAuthLifecycle
from scripts import clientplatform_prepare_production_env as prepare_env
from services.db.schema import (
    clientplatform_ad_connections,
    clientplatform_promotions,
    clientplatform_tenancy,
)


_REQUIRED_ENV = """\
CLIENTPLATFORM_DOMAIN=clientplatform.example.test
CLIENTPLATFORM_STORAGE_BUCKET=clientplatform-production
CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT=https://s3.example.test
CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION=ru-1
CLIENTPLATFORM_SECRET_S3_ACCESS_KEY=access-secret
CLIENTPLATFORM_SECRET_S3_SECRET_KEY=secret-secret
"""


class FakeTransport:
    def __init__(self, status: int, payload: object):
        self.status = status
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def request(self, *, method, url, headers, body=None, timeout=20.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        return self.status, {}, json.dumps(self.payload).encode("utf-8")


class AdCredentialLifecycleTests(unittest.TestCase):
    def test_production_vault_never_generates_a_missing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "identity.txt"
            with mock.patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "CLIENTPLATFORM_AD_CREDENTIAL_ALLOW_GENERATE": "1",
                },
                clear=False,
            ):
                vault = AgeAdCredentialVault(missing)
                with self.assertRaisesRegex(
                    AdCredentialVaultError,
                    "must be provisioned",
                ):
                    vault.seal("secret")
            self.assertFalse(missing.exists())

    def test_yandex_revocation_never_logs_or_echoes_token(self) -> None:
        transport = FakeTransport(200, {"status": "ok"})
        lifecycle = YandexOAuthLifecycle(
            client_id="client-id",
            client_secret="client-secret",
            transport=transport,
        )
        result = lifecycle.revoke(access_token="private-access-token")
        self.assertTrue(result.provider_revoked)
        self.assertTrue(result.local_erasure_allowed)
        self.assertEqual(len(transport.calls), 1)
        form = parse_qs(transport.calls[0]["body"].decode("ascii"))
        self.assertEqual(form["access_token"], ["private-access-token"])
        self.assertNotIn("private-access-token", str(transport.calls[0]["headers"]))

    def test_unsupported_remote_revocation_still_allows_local_erasure(self) -> None:
        transport = FakeTransport(400, {"error": "unsupported_token_type"})
        result = YandexOAuthLifecycle(
            client_id="client-id",
            client_secret="client-secret",
            transport=transport,
        ).revoke(access_token="private-access-token")
        self.assertFalse(result.provider_revoked)
        self.assertTrue(result.local_erasure_allowed)


class AdWorkerAndDisconnectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_promotions.ensure(self.conn)
        clientplatform_ad_connections.ensure(self.conn)
        self.vault = InMemoryAdCredentialVault()
        self.tenancy = TenancyRepository(self.conn)
        access = self.tenancy.create_business(owner_user_id=9001, name="Мастер")
        self.owner = self.tenancy.resolve_context(
            user_id=9001,
            business_id=access.business.id,
        )
        self.repository = AdConnectionRepository(self.conn, vault=self.vault)
        state = new_oauth_state()
        verifier = new_pkce_verifier()
        self.repository.create_oauth_session(
            actor=self.owner,
            provider=AdProvider.YANDEX_DIRECT,
            state=state,
            verifier=verifier,
        )
        session, _ = self.repository.consume_oauth_session(state=state)
        self.connection = self.repository.activate_oauth_connection(
            session=session,
            external_account_id="100500",
            external_login="master-account",
            token_bundle_json=YandexTokenBundle(
                access_token="old-token",
                token_type="bearer",
                expires_in=3600,
                refresh_token="refresh-token",
                scope=("direct:api",),
            ).to_json(),
            permissions=("campaigns.read", "adgroups.write", "ads.write"),
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_worker_refresh_replaces_only_encrypted_material(self) -> None:
        store = AdWorkerStore(self.conn, vault=self.vault)
        connection, old_json = store.load_active(
            business_id=self.owner.business_id,
            connection_id=self.connection.id,
        )
        self.assertEqual(YandexTokenBundle.from_json(old_json).access_token, "old-token")
        store.replace_token_bundle(
            connection=connection,
            token_bundle_json=YandexTokenBundle(
                access_token="new-token",
                token_type="bearer",
                expires_in=7200,
                refresh_token="new-refresh-token",
                scope=("direct:api",),
            ).to_json(),
        )
        ciphertext = self.conn.execute(
            "SELECT credential_ciphertext FROM ad_connections WHERE id=?",
            (connection.id,),
        ).fetchone()[0]
        self.assertNotIn("new-token", ciphertext)
        _, new_json = store.load_active(
            business_id=self.owner.business_id,
            connection_id=connection.id,
        )
        self.assertEqual(YandexTokenBundle.from_json(new_json).access_token, "new-token")

    def test_disconnect_erases_token_and_cancels_unsubmitted_jobs(self) -> None:
        now = "2026-08-05T10:00:00+00:00"
        promotion_id = "11111111-1111-4111-8111-111111111111"
        slot_id = "22222222-2222-4222-8222-222222222222"
        offering_id = "33333333-3333-4333-8333-333333333333"
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_offerings(
                id TEXT NOT NULL,
                business_id TEXT NOT NULL,
                PRIMARY KEY(id, business_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS booking_slots(
                id TEXT NOT NULL,
                business_id TEXT NOT NULL,
                PRIMARY KEY(id, business_id)
            )
            """
        )
        self.conn.execute(
            "INSERT INTO business_offerings(id, business_id) VALUES(?, ?)",
            (offering_id, self.owner.business_id),
        )
        self.conn.execute(
            "INSERT INTO booking_slots(id, business_id) VALUES(?, ?)",
            (slot_id, self.owner.business_id),
        )
        self.conn.execute(
            """
            INSERT INTO promotion_campaigns(
                id, business_id, offering_id, booking_slot_id, channel,
                source_token, creative_id, headline, primary_text, description,
                cta, creative_style, status, created_by_member_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'website', 'source-token', 'creative-id',
                     'Title', 'Text', 'Description', 'Записаться', 'direct', 'active',
                     ?, ?, ?)
            """,
            (
                promotion_id,
                self.owner.business_id,
                offering_id,
                slot_id,
                self.owner.membership_id,
                now,
                now,
            ),
        )
        job_id = "44444444-4444-4444-8444-444444444444"
        self.conn.execute(
            """
            INSERT INTO ad_publication_jobs(
                id, business_id, promotion_campaign_id, connection_id,
                external_campaign_id, external_campaign_name, region_ids_json,
                source_url, title, text, status, idempotency_key,
                external_ad_group_id, external_ad_id, attempts, available_at,
                locked_at, lock_token, last_error_code, created_by_member_id,
                created_at, updated_at, submitted_at, dead_at
            ) VALUES(?, ?, ?, ?, '6001', 'Campaign', '[47]',
                     'https://t.me/bot?start=source', 'Title', 'Text', 'queued',
                     'adjob_0123456789abcdef0123456789abcdef', NULL, NULL, 0, ?,
                     NULL, NULL, NULL, ?, ?, ?, NULL, NULL)
            """,
            (
                job_id,
                self.owner.business_id,
                promotion_id,
                self.connection.id,
                now,
                self.owner.membership_id,
                now,
                now,
            ),
        )
        store = AdConnectionLifecycleStore(self.conn, vault=self.vault)
        connection, token_json = store.load_for_disconnect(
            actor=self.owner,
            connection_id=self.connection.id,
        )
        self.assertEqual(YandexTokenBundle.from_json(token_json).access_token, "old-token")
        revoked = store.erase_after_provider_revocation(
            actor=self.owner,
            connection_id=connection.id,
            now=now,
        )
        self.assertEqual(revoked.status, AdConnectionStatus.REVOKED)
        row = self.conn.execute(
            "SELECT credential_ciphertext FROM ad_connections WHERE id=?",
            (connection.id,),
        ).fetchone()
        self.assertEqual(row[0], "")
        job = self.conn.execute(
            "SELECT status, last_error_code FROM ad_publication_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        self.assertEqual(tuple(job), ("cancelled", "connection_revoked"))


class AdProductionContractTests(unittest.TestCase):
    def test_disabled_feature_is_default_and_has_exact_safe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clientplatform.env"
            path.write_text(_REQUIRED_ENV, encoding="utf-8")
            os.chmod(path, 0o600)
            prepare_env.prepare(path)
            payload = path.read_text(encoding="utf-8")
        self.assertIn("CLIENTPLATFORM_AD_CONNECTIONS_ENABLED=0", payload)
        self.assertIn(
            "CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI="
            "https://clientplatform.example.test/oauth/yandex-direct/callback",
            payload,
        )
        self.assertIn(
            "CLIENTPLATFORM_AD_CREDENTIAL_IDENTITY_FILE="
            "/run/secrets/clientplatform-ad/identity.txt",
            payload,
        )

    def test_enabled_feature_requires_client_id_secret_and_exact_redirect(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clientplatform.env"
            path.write_text(
                _REQUIRED_ENV + "CLIENTPLATFORM_AD_CONNECTIONS_ENABLED=1\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(
                prepare_env.EnvironmentPreparationError,
                "missing_clientplatform_yandex_direct_client_id",
            ):
                prepare_env.prepare(path)

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clientplatform.env"
            path.write_text(
                _REQUIRED_ENV
                + "CLIENTPLATFORM_AD_CONNECTIONS_ENABLED=1\n"
                + "CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID=client-id\n"
                + "CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET=client-secret\n"
                + "CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI=https://wrong.test/callback\n"
                + "CLIENTPLATFORM_AD_CREDENTIAL_IDENTITY_FILE="
                + "/run/secrets/clientplatform-ad/identity.txt\n"
                + "CLIENTPLATFORM_AD_CREDENTIAL_HOST_DIR=/var/lib/clientplatform/ad-secrets\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(
                prepare_env.EnvironmentPreparationError,
                "mismatched_clientplatform_ad_oauth_redirect_uri",
            ):
                prepare_env.prepare(path)

    def test_compose_and_caddy_expose_only_the_oauth_callback(self) -> None:
        root = Path(prepare_env.__file__).resolve().parents[1]
        compose = (root / "deploy/clientplatform/compose.production.yml").read_text(
            encoding="utf-8"
        )
        caddy = (root / "deploy/clientplatform/Caddyfile").read_text(
            encoding="utf-8"
        )
        configure = (
            root / "deploy/clientplatform/configure-ad-credential-age.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "/var/lib/clientplatform/ad-secrets}:/run/secrets/clientplatform-ad:ro",
            compose,
        )
        self.assertIn("path /oauth/yandex-direct/callback", caddy)
        self.assertNotIn("/oauth/*", caddy)
        self.assertIn("CLIENTPLATFORM_AD_CREDENTIAL_AGE_OK", configure)
        self.assertNotIn("cat \"$IDENTITY\"", configure)


if __name__ == "__main__":
    unittest.main()
