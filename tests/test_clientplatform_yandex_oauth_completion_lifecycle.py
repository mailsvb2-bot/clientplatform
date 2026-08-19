from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clientplatform.application.ad_connections import complete_yandex_direct_oauth
from clientplatform.domain.ad_connections import (
    AdConnectionInvariantViolation,
    AdProvider,
    new_oauth_state,
    new_pkce_verifier,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.ad_connection_repository import AdConnectionRepository
from clientplatform.infrastructure.ad_credential_vault import InMemoryAdCredentialVault
from clientplatform.integrations.yandex_direct import (
    YandexAccountIdentity,
    YandexDirectError,
    YandexTokenBundle,
)
from services.db import core as db_core
from services.db.schema import clientplatform_ad_connections, clientplatform_tenancy


class _SequencedProvider:
    def __init__(self, *, expected_verifier: str, db_path: Path) -> None:
        self.expected_verifier = expected_verifier
        self.db_path = db_path
        self.exchange_codes: list[str] = []
        self.identity_calls = 0

    def _prove_provider_io_has_no_db_writer(self, value: str) -> None:
        # This write happens synchronously inside the simulated provider call.
        # If complete_yandex_direct_oauth holds the OAuth write transaction open
        # across provider I/O, SQLite fails here with "database is locked".
        conn = sqlite3.connect(self.db_path, timeout=0.1)
        try:
            conn.execute(
                "INSERT INTO oauth_provider_io_probe(value) VALUES(?)",
                (value,),
            )
            conn.commit()
        finally:
            conn.close()

    def exchange_code(self, *, code: str, verifier: str) -> YandexTokenBundle:
        self.exchange_codes.append(code)
        if verifier != self.expected_verifier:
            raise AssertionError("PKCE verifier changed across OAuth completion attempts")
        self._prove_provider_io_has_no_db_writer(code)
        if code == "rejected-first-code":
            raise YandexDirectError("provider_bad_verification_code")
        return YandexTokenBundle(
            access_token="accepted-access-token",
            token_type="bearer",
            expires_in=3600,
            refresh_token="accepted-refresh-token",
            scope=("direct:api",),
        )

    def account_identity(self, *, access_token: str) -> YandexAccountIdentity:
        self.identity_calls += 1
        if access_token != "accepted-access-token":
            raise AssertionError("unexpected access token reached identity proof")
        return YandexAccountIdentity(account_id="100500", login="owner-login")


class YandexOauthCompletionLifecycleTests(unittest.TestCase):
    def test_provider_rejection_releases_lease_then_success_consumes_state_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db_path = Path(raw) / "clientplatform.sqlite3"
            vault = InMemoryAdCredentialVault()
            verifier = new_pkce_verifier()
            state = new_oauth_state()

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            clientplatform_tenancy.ensure(conn)
            clientplatform_ad_connections.ensure(conn)
            conn.execute(
                "CREATE TABLE oauth_provider_io_probe(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            tenancy = TenancyRepository(conn)
            created = tenancy.create_business(owner_user_id=101, name="OAuth retry business")
            actor = tenancy.resolve_context(
                user_id=101,
                business_id=created.business.id,
            )
            AdConnectionRepository(conn, vault=vault).create_oauth_session(
                actor=actor,
                provider=AdProvider.YANDEX_DIRECT,
                state=state,
                verifier=verifier,
            )
            conn.commit()
            conn.close()

            provider = _SequencedProvider(expected_verifier=verifier, db_path=db_path)
            with (
                mock.patch.object(db_core, "DB_PATH", db_path),
                mock.patch.object(db_core, "is_postgres_enabled", return_value=False),
            ):
                with self.assertRaisesRegex(
                    YandexDirectError,
                    "provider_bad_verification_code",
                ):
                    complete_yandex_direct_oauth(
                        state=state,
                        code="rejected-first-code",
                        vault=vault,
                        provider=provider,  # type: ignore[arg-type]
                    )

                check = sqlite3.connect(db_path)
                first_row = check.execute(
                    """
                    SELECT consumed_at, completion_attempt_id,
                           completion_attempt_expires_at
                    FROM ad_oauth_sessions
                    """
                ).fetchone()
                first_probe_count = check.execute(
                    "SELECT COUNT(*) FROM oauth_provider_io_probe"
                ).fetchone()[0]
                check.close()
                self.assertEqual(first_row, (None, None, None))
                self.assertEqual(first_probe_count, 1)

                completion = complete_yandex_direct_oauth(
                    state=state,
                    code="accepted-second-code",
                    vault=vault,
                    provider=provider,  # type: ignore[arg-type]
                )

                self.assertEqual(completion.user_id, 101)
                self.assertEqual(completion.connection.external_account_id, "100500")
                self.assertEqual(completion.connection.external_login, "owner-login")

                check = sqlite3.connect(db_path)
                final_row = check.execute(
                    """
                    SELECT consumed_at, completion_attempt_id,
                           completion_attempt_expires_at
                    FROM ad_oauth_sessions
                    """
                ).fetchone()
                connection_count = check.execute(
                    "SELECT COUNT(*) FROM ad_connections WHERE status='active'"
                ).fetchone()[0]
                probe_count = check.execute(
                    "SELECT COUNT(*) FROM oauth_provider_io_probe"
                ).fetchone()[0]
                check.close()
                self.assertIsNotNone(final_row[0])
                self.assertIsNone(final_row[1])
                self.assertIsNone(final_row[2])
                self.assertEqual(connection_count, 1)
                self.assertEqual(probe_count, 2)

                with self.assertRaisesRegex(
                    AdConnectionInvariantViolation,
                    "invalid, expired or already used",
                ):
                    complete_yandex_direct_oauth(
                        state=state,
                        code="accepted-second-code",
                        vault=vault,
                        provider=provider,  # type: ignore[arg-type]
                    )

            self.assertEqual(
                provider.exchange_codes,
                ["rejected-first-code", "accepted-second-code"],
            )
            self.assertEqual(provider.identity_calls, 1)


if __name__ == "__main__":
    unittest.main()
