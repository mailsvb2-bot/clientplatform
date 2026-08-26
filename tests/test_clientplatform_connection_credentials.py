from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from clientplatform.domain.connections import ConnectionInvariantViolation
from clientplatform.domain.messenger_channels import CustomerChannelLinkRejected
from clientplatform.infrastructure import ConnectionRepository
from clientplatform.infrastructure.connection_credentials import ConnectionCredentialStore
from clientplatform.infrastructure.managed_bot_credentials import InMemoryManagedBotCredentialVault
from clientplatform.infrastructure.messenger_channel_repository import MessengerChannelRepository
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from services.db.schema import (
    clientplatform_connections,
    clientplatform_messenger_channels,
    clientplatform_tenancy,
)


class ClientPlatformConnectionCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        clientplatform_messenger_channels.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        first = tenancy.create_business(owner_user_id=101, name="Практика")
        second = tenancy.create_business(owner_user_id=202, name="Школа")
        self.first = tenancy.resolve_context(user_id=101, business_id=first.business.id)
        self.second = tenancy.resolve_context(user_id=202, business_id=second.business.id)
        self.vault = InMemoryManagedBotCredentialVault()
        self.store = ConnectionCredentialStore(self.conn, vault=self.vault)

    def tearDown(self) -> None:
        self.conn.close()

    def test_native_token_is_encrypted_rotatable_and_runtime_resolvable(self) -> None:
        first_ref = self.store.put(
            actor=self.first,
            platform="max",
            external_account_id="900001",
            purpose="provider_token",
            plaintext="max-secret-one",
        )
        second_ref = self.store.put(
            actor=self.first,
            platform="max",
            external_account_id="900001",
            purpose="provider_token",
            plaintext="max-secret-two",
        )
        self.assertEqual(first_ref, second_ref)
        row = self.conn.execute(
            "SELECT ciphertext FROM connection_credentials"
        ).fetchone()
        self.assertNotIn("max-secret-two", str(row["ciphertext"]))

        @contextmanager
        def local_db_ro():
            yield self.conn

        provider = EnvironmentCredentialProvider(connection_vault=self.vault)
        with patch("clientplatform.runtime.secrets.get_db_ro", local_db_ro):
            self.assertEqual(provider.resolve(second_ref), "max-secret-two")

    def test_connection_rejects_other_business_vault_reference(self) -> None:
        reference = self.store.put(
            actor=self.first,
            platform="vk",
            external_account_id="44",
            purpose="provider_token",
            plaintext="vk-secret",
        )
        with self.assertRaisesRegex(
            ConnectionInvariantViolation, "another business"
        ):
            ConnectionRepository(self.conn).create_connection(
                actor=self.second,
                platform="vk",
                connection_type="vk_community",
                external_account_id="44",
                credential_reference=reference,
            )

    def test_ingress_route_rejects_other_business_webhook_reference(self) -> None:
        own_token_ref = self.store.put(
            actor=self.second,
            platform="max",
            external_account_id="55",
            purpose="provider_token",
            plaintext="own-token",
        )
        connection = ConnectionRepository(self.conn).create_connection(
            actor=self.second,
            platform="max",
            connection_type="max_personal_bot",
            external_account_id="55",
            credential_reference=own_token_ref,
        )
        ConnectionRepository(self.conn).activate_connection(
            actor=self.second,
            connection_id=connection.id,
        )
        foreign_webhook_ref = self.store.put(
            actor=self.first,
            platform="max",
            external_account_id="55",
            purpose="webhook_secret",
            plaintext="foreign-webhook-secret",
        )
        with self.assertRaisesRegex(
            CustomerChannelLinkRejected, "another business"
        ):
            MessengerChannelRepository(self.conn).register_route(
                actor=self.second,
                connection_id=connection.id,
                external_route_id="55",
                webhook_secret_reference=foreign_webhook_ref,
            )


if __name__ == "__main__":
    unittest.main()
