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
    def __init__(self, *, expected_verifier: str) -> None:
        self.expected_verifier = expected_verifier
        self.exchange_codes: list[str] = []
        self.identity_calls = 0

    def exchange_code(self, *, code: str, verifier: str) -> YandexTokenBundle:
        self.exchange_codes.append(code)
        if verifier != self.expected_verifier:
            raise AssertionError("PKCE verifier changed across OAuth completion attempts")
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
    def test_provider_rejection_rolls_back_state_then_success_consumes_it_once(self) -> None:
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

            provider = _SequencedProvider(expected_verifier=verifier)
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
                first_consumed_at = check.execute(
                    "SELECT consumed_at FROM ad_oauth_sessions"
                ).fetchone()[0]
                check.close()
                self.assertIsNone(first_consumed_at)

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
                consumed_at = check.execute(
                    "SELECT consumed_at FROM ad_oauth_sessions"
                ).fetchone()[0]
                connection_count = check.execute(
                    "SELECT COUNT(*) FROM ad_connections WHERE status='active'"
                ).fetchone()[0]
                check.close()
                self.assertIsNotNone(consumed_at)
                self.assertEqual(connection_count, 1)

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
