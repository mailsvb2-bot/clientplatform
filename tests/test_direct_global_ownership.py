from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

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


def test_direct_account_cannot_be_claimed_by_another_tenant() -> None:
    conn = _fresh_db()
    _insert_connection(conn, business_id="tenant-a")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_connection(conn, business_id="tenant-b")


def test_attention_and_disabled_keep_ownership_until_revoked() -> None:
    conn = _fresh_db()
    connection_id = _insert_connection(conn, business_id="tenant-a")

    for status in ("attention", "disabled"):
        conn.execute(
            "UPDATE ad_connections SET status=? WHERE id=?",
            (status, connection_id),
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_connection(conn, business_id=f"tenant-{status}")

    conn.execute("UPDATE ad_connections SET status='revoked' WHERE id=?", (connection_id,))
    _insert_connection(conn, business_id="tenant-b")


def test_legacy_connection_is_quarantined_and_reconnect_promotes_identity() -> None:
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
    conn.execute(
        """
        INSERT INTO ad_connections(
            id, business_id, provider, external_account_id, external_login,
            credential_ciphertext, permissions_json, status,
            created_by_member_id, created_at, updated_at
        ) VALUES('legacy', 'tenant-a', 'yandex_direct', 'oauth-user-id', 'oauth-login',
                 'sealed', '[]', 'active', 'member-a', 'old', 'old')
        """
    )

    apply(conn)
    row = conn.execute(
        "SELECT status, identity_source, last_error_code FROM ad_connections WHERE id='legacy'"
    ).fetchone()
    assert tuple(row) == (
        "disabled",
        "legacy_oauth",
        "direct_identity_reverification_required",
    )

    conn.execute(
        "UPDATE ad_connections SET status='active', updated_at='new' WHERE id='legacy'"
    )
    row = conn.execute(
        "SELECT status, identity_source FROM ad_connections WHERE id='legacy'"
    ).fetchone()
    assert tuple(row) == ("active", "direct_client_id")


def test_racing_tenants_have_exactly_one_direct_owner(tmp_path: Path) -> None:
    path = tmp_path / "direct-ownership.sqlite3"
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

    assert sorted(results) == ["claimed", "conflict"]


class _IdentityProvider(ModeratingYandexDirectProvider):
    def __init__(self, response):
        self.response = response
        self.last_payload = None

    def _direct_call(self, *, service, token, payload):
        assert service == "clients"
        assert token == "token"
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


def test_direct_identity_uses_client_id_and_requests_type() -> None:
    provider = _IdentityProvider(_client(client_id=4242))

    identity = provider.account_identity(access_token="token")

    assert identity.account_id == "4242"
    assert identity.login == "representative-login"
    assert "Type" in provider.last_payload["params"]["FieldNames"]


def test_direct_identity_rejects_agency_fail_closed() -> None:
    provider = _IdentityProvider(_client(account_type="AGENCY"))

    with pytest.raises(YandexDirectError) as raised:
        provider.account_identity(access_token="token")

    assert raised.value.code == "direct_agency_account_ambiguous"


def test_direct_identity_rejects_ambiguous_client_list() -> None:
    provider = _IdentityProvider({"Clients": _client()["Clients"] * 2})

    with pytest.raises(YandexDirectError) as raised:
        provider.account_identity(access_token="token")

    assert raised.value.code == "direct_account_identity_ambiguous"
