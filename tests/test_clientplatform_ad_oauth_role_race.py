from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from clientplatform.application import ad_connections as application
from clientplatform.domain.tenancy import PlatformRole, TenantAccessDenied
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.ad_credential_vault import InMemoryAdCredentialVault
from clientplatform.integrations.yandex_direct import (
    YandexAccountIdentity,
    YandexTokenBundle,
)
from services.db.schema import (
    clientplatform_ad_connections,
    clientplatform_promotions,
    clientplatform_tenancy,
)


class FakeProvider:
    def __init__(self) -> None:
        self.state = ""

    def authorization_url(self, *, state: str, verifier: str) -> str:
        self.state = state
        self.asserted_verifier = verifier
        return "https://oauth.example.test/authorize"

    def exchange_code(self, *, code: str, verifier: str) -> YandexTokenBundle:
        if code != "provider-code" or verifier != self.asserted_verifier:
            raise AssertionError("unexpected OAuth exchange input")
        return YandexTokenBundle(
            access_token="access-token",
            token_type="bearer",
            expires_in=3600,
            refresh_token="refresh-token",
            scope=("direct:api",),
        )

    def account_identity(self, *, access_token: str) -> YandexAccountIdentity:
        if access_token != "access-token":
            raise AssertionError("unexpected access token")
        return YandexAccountIdentity(account_id="100500", login="administrator")


class AdOAuthRoleRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_promotions.ensure(self.conn)
        clientplatform_ad_connections.ensure(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        access = self.tenancy.create_business(owner_user_id=101, name="Мастер")
        self.owner = self.tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.tenancy.grant_member(
            actor=self.owner,
            user_id=202,
            role=PlatformRole.ADMINISTRATOR,
        )
        self.administrator = self.tenancy.resolve_context(
            user_id=202,
            business_id=self.owner.business_id,
        )
        self.vault = InMemoryAdCredentialVault()
        self.provider = FakeProvider()

    def tearDown(self) -> None:
        self.conn.close()

    def test_revoked_administrator_cannot_complete_started_oauth(self) -> None:
        with mock.patch.object(application, "get_db", return_value=self.conn):
            application.start_yandex_direct_oauth(
                actor=self.administrator,
                vault=self.vault,
                provider=self.provider,
            )

            self.tenancy.revoke_member(
                actor=self.owner,
                user_id=self.administrator.user_id,
            )

            with self.assertRaises(TenantAccessDenied):
                application.complete_yandex_direct_oauth(
                    state=self.provider.state,
                    code="provider-code",
                    vault=self.vault,
                    provider=self.provider,
                )

        count = self.conn.execute("SELECT COUNT(*) FROM ad_connections").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
