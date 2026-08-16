from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from clientplatform.integrations.yandex_direct import YandexDirectError
from clientplatform.integrations.yandex_direct_moderation import ModeratingYandexDirectProvider
from services.db.schema import clientplatform_ad_connections
from services.migrations.clientplatform_direct_global_ownership_v1 import apply


_ACCOUNT_ID = "123456789"


def _insert_connection(
    conn: sqlite3.Connection,
    *,
    business_id: str,
    account_id: str = _ACCOUNT_ID,
    status: str = "active",
    identity_source: str = "direct_client_id",
) -> str:
    connection_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO ad_connections(
            id, business_id, provider, external_account_id, external_login,
            identity_source, credential_ciphertext, permissions_json, status,
            created_by_member_id, created_at, updated_at
        ) VALUES(?, ?, 'yandex_direct', ?, ?, ?, 'sealed', '[]', ?, ?, ?, ?)
        """,
        (
            connection_id,
            business_id,
            account_id,
            f"login-{business_id}",
            identity_source,
            status,
            str(uuid4()),
            "2026-08-16T00:00:00+00:00",
            "2026-08-16T00:00:00+00:00",
        ),
    )
    return connection_id


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    clientplatform_ad_connections.ensure(conn)
    apply(conn)
    return conn


def _legacy_db(*, rows: int = 1) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE ad_connections(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            external_account_id TEXT NOT NULL,
            external_login TEXT NOT NULL,
            credential_ciphertext TEXT NOT NULL,
            permissions_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_success_at TEXT,
            last_error_at TEXT,
            last_error_code TEXT,
            UNIQUE(business_id, provider, external_account_id)
        )
        """
    )
    for index in range(rows):
        conn.execute(
            """
            INSERT INTO ad_connections(
                id, business_id, provider, external_account_id, external_login,
                credential_ciphertext, permissions_json, status,
                created_by_member_id, created_at, updated_at
            ) VALUES(?, 'tenant-a', 'yandex_direct', ?, ?, 'sealed-legacy',
                     '["legacy"]', 'active', ?, 'old', 'old')
            """,
            (
                f"legacy-{index}",
                f"oauth-user-{index}",
                f"oauth-login-{index}",
                f"member-{index}",
            ),
        )
    apply(conn)
    return conn


class DirectOwnershipDatabaseTests(unittest.TestCase):
    def test_direct_account_cannot_be_claimed_by_another_tenant(self) -> None:
        conn = _fresh_db()
        self.addCleanup(conn.close)
        _insert_connection(conn, business_id="tenant-a")

        with self.assertRaises(sqlite3.IntegrityError):
            _insert_connection(conn, business_id="tenant-b")

    def test_attention_and_disabled_keep_ownership_until_revoked(self) -> None:
        conn = _fresh_db()
        self.addCleanup(conn.close)
        connection_id = _insert_connection(conn, business_id="tenant-a")

        for status in ("attention", "disabled"):
            conn.execute(
                "UPDATE ad_connections SET status=? WHERE id=?",
                (status, connection_id),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                _insert_connection(conn, business_id=f"tenant-{status}")

        conn.execute(
            "UPDATE ad_connections SET status='revoked' WHERE id=?",
            (connection_id,),
        )
        _insert_connection(conn, business_id="tenant-b")

    def test_verified_same_tenant_reconnect_keeps_single_owner(self) -> None:
        conn = _fresh_db()
        self.addCleanup(conn.close)
        connection_id = _insert_connection(conn, business_id="tenant-a")
        conn.execute(
            "UPDATE ad_connections SET status='attention' WHERE id=?",
            (connection_id,),
        )
        conn.execute(
            """
            INSERT INTO ad_connections(
                id, business_id, provider, external_account_id, external_login,
                identity_source, credential_ciphertext, permissions_json, status,
                created_by_member_id, created_at, updated_at
            ) VALUES(?, 'tenant-a', 'yandex_direct', ?, 'new-login',
                     'direct_client_id', 'new-sealed', '[]', 'active', ?, 'new', 'new')
            ON CONFLICT(business_id, provider, external_account_id) DO UPDATE SET
                external_login=excluded.external_login,
                credential_ciphertext=excluded.credential_ciphertext,
                status='active',
                updated_at=excluded.updated_at
            """,
            (str(uuid4()), _ACCOUNT_ID, str(uuid4())),
        )
        rows = conn.execute(
            """
            SELECT id, status, external_login FROM ad_connections
            WHERE business_id='tenant-a' AND external_account_id=?
            """,
            (_ACCOUNT_ID,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], connection_id)
        self.assertEqual(rows[0]["status"], "active")
        self.assertEqual(rows[0]["external_login"], "new-login")

    def test_legacy_owner_blocks_new_tenant_until_reverification(self) -> None:
        conn = _legacy_db()
        self.addCleanup(conn.close)
        legacy = conn.execute(
            """
            SELECT status, identity_source, last_error_code
            FROM ad_connections WHERE id='legacy-0'
            """
        ).fetchone()
        self.assertEqual(
            tuple(legacy),
            (
                "disabled",
                "legacy_oauth",
                "direct_identity_reverification_required",
            ),
        )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "direct_identity_reverification_pending",
        ):
            _insert_connection(conn, business_id="tenant-b", account_id="900001")

    def test_legacy_reconnect_claims_real_client_id_and_erases_old_credential(self) -> None:
        conn = _legacy_db()
        self.addCleanup(conn.close)

        verified_id = _insert_connection(
            conn,
            business_id="tenant-a",
            account_id="424242",
        )

        legacy = conn.execute(
            """
            SELECT status, identity_source, credential_ciphertext,
                   permissions_json, last_error_code
            FROM ad_connections WHERE id='legacy-0'
            """
        ).fetchone()
        self.assertEqual(
            tuple(legacy),
            (
                "revoked",
                "legacy_oauth",
                "",
                "[]",
                "direct_identity_reverified",
            ),
        )
        verified = conn.execute(
            """
            SELECT external_account_id, identity_source, status
            FROM ad_connections WHERE id=?
            """,
            (verified_id,),
        ).fetchone()
        self.assertEqual(tuple(verified), ("424242", "direct_client_id", "active"))

        # Once the last legacy row is retired, unrelated tenants may connect their
        # own different Direct cabinets.
        _insert_connection(conn, business_id="tenant-b", account_id="900001")

    def test_ambiguous_legacy_rows_fail_closed(self) -> None:
        conn = _legacy_db(rows=2)
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "direct_identity_reverification_ambiguous",
        ):
            _insert_connection(conn, business_id="tenant-a", account_id="424242")

    def test_legacy_row_cannot_be_reactivated_by_status_only(self) -> None:
        conn = _legacy_db()
        self.addCleanup(conn.close)
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "direct_identity_reverification_required",
        ):
            conn.execute(
                "UPDATE ad_connections SET status='active' WHERE id='legacy-0'"
            )

    def test_racing_tenants_have_exactly_one_direct_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "direct-ownership.sqlite3"
            setup = sqlite3.connect(path)
            setup.row_factory = sqlite3.Row
            setup.execute("PRAGMA journal_mode=WAL")
            clientplatform_ad_connections.ensure(setup)
            apply(setup)
            setup.commit()
            setup.close()

            barrier = threading.Barrier(2)

            def claim(business_id: str) -> str:
                conn = sqlite3.connect(path, timeout=5.0)
                try:
                    conn.execute("PRAGMA busy_timeout=5000")
                    barrier.wait(timeout=5.0)
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        _insert_connection(conn, business_id=business_id)
                        conn.commit()
                        return "claimed"
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        return "conflict"
                finally:
                    conn.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(claim, ("tenant-a", "tenant-b")))

        self.assertEqual(sorted(results), ["claimed", "conflict"])


class _IdentityProvider(ModeratingYandexDirectProvider):
    def __init__(self, response):
        self.response = response
        self.last_payload = None

    def _direct_call(self, *, service, token, payload):
        if service != "clients":
            raise AssertionError(service)
        if token != "token":
            raise AssertionError(token)
        self.last_payload = payload
        return self.response


def _client(*, account_type: str = "CLIENT", client_id: int = 42):
    return {
        "Clients": [
            {
                "ClientId": client_id,
                "Login": "representative-login",
                "Type": account_type,
                "Archived": "NO",
                "Grants": [{"Privilege": "EDIT_CAMPAIGNS", "Value": "YES"}],
            }
        ]
    }


class DirectIdentityTests(unittest.TestCase):
    def test_direct_identity_uses_client_id_and_requests_type(self) -> None:
        provider = _IdentityProvider(_client(client_id=4242))

        identity = provider.account_identity(access_token="token")

        self.assertEqual(identity.account_id, "4242")
        self.assertEqual(identity.login, "representative-login")
        self.assertIn("Type", provider.last_payload["params"]["FieldNames"])

    def test_subclient_is_a_concrete_advertiser_identity(self) -> None:
        provider = _IdentityProvider(_client(account_type="SUBCLIENT", client_id=77))
        identity = provider.account_identity(access_token="token")
        self.assertEqual(identity.account_id, "77")

    def test_agency_is_rejected_fail_closed(self) -> None:
        provider = _IdentityProvider(_client(account_type="AGENCY"))

        with self.assertRaises(YandexDirectError) as raised:
            provider.account_identity(access_token="token")

        self.assertEqual(raised.exception.code, "direct_agency_account_ambiguous")

    def test_missing_type_is_rejected_fail_closed(self) -> None:
        response = _client()
        del response["Clients"][0]["Type"]
        provider = _IdentityProvider(response)

        with self.assertRaises(YandexDirectError) as raised:
            provider.account_identity(access_token="token")

        self.assertEqual(raised.exception.code, "direct_account_type_unsupported")

    def test_ambiguous_client_list_is_rejected(self) -> None:
        provider = _IdentityProvider({"Clients": _client()["Clients"] * 2})

        with self.assertRaises(YandexDirectError) as raised:
            provider.account_identity(access_token="token")

        self.assertEqual(raised.exception.code, "direct_account_identity_ambiguous")


class DirectOnboardingContractTests(unittest.TestCase):
    def test_onboarding_has_two_explicit_user_owned_paths(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "handlers"
            / "clientplatform_yandex_screen_code.py"
        ).read_text(encoding="utf-8")
        self.assertIn("Подключить мой кабинет", source)
        self.assertIn("У меня ещё нет кабинета", source)
        self.assertNotIn("общий кабинет ClientPlatform", source)
        self.assertNotIn("mailsvb2", source.lower())


if __name__ == "__main__":
    unittest.main()
