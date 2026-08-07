from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.tenancy import TenantAccessDenied
from clientplatform.infrastructure.managed_bot_credentials import (
    InMemoryManagedBotCredentialVault,
    ManagedBotCredentialStore,
)
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db.schema import clientplatform_connections, clientplatform_tenancy


class ClientPlatformManagedBotCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        first = tenancy.create_business(owner_user_id=101, name="Практика")
        second = tenancy.create_business(owner_user_id=202, name="Школа")
        self.first = tenancy.resolve_context(
            user_id=101,
            business_id=first.business.id,
        )
        self.second = tenancy.resolve_context(
            user_id=202,
            business_id=second.business.id,
        )
        self.vault = InMemoryManagedBotCredentialVault()
        self.store = ManagedBotCredentialStore(self.conn, vault=self.vault)

    def tearDown(self) -> None:
        self.conn.close()

    def test_token_is_encrypted_and_resolved_only_through_vault_reference(self) -> None:
        token = "900001:" + ("A" * 40)
        reference = self.store.put(
            actor=self.first,
            external_bot_id="900001",
            token=token,
            now="2026-08-07T12:00:00+00:00",
        )

        self.assertTrue(
            reference.startswith(f"vault://managed-bot/{self.first.business_id}/")
        )
        self.assertNotIn(token, reference)
        row = self.conn.execute(
            "SELECT ciphertext,status FROM managed_bot_credentials"
        ).fetchone()
        self.assertEqual(row["status"], "active")
        self.assertNotEqual(row["ciphertext"], token)
        self.assertNotIn(token, row["ciphertext"])
        self.assertEqual(self.store.resolve(reference), token)

    def test_rotation_reuses_reference_and_updates_ciphertext(self) -> None:
        first_token = "900001:" + ("A" * 40)
        second_token = "900001:" + ("B" * 40)
        first_reference = self.store.put(
            actor=self.first,
            external_bot_id="900001",
            token=first_token,
        )
        second_reference = self.store.put(
            actor=self.first,
            external_bot_id="900001",
            token=second_token,
        )

        self.assertEqual(first_reference, second_reference)
        self.assertEqual(self.store.resolve(second_reference), second_token)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM managed_bot_credentials").fetchone()[0],
            1,
        )

    def test_other_business_cannot_revoke_credential(self) -> None:
        reference = self.store.put(
            actor=self.first,
            external_bot_id="900001",
            token="900001:" + ("A" * 40),
        )
        with self.assertRaises(TenantAccessDenied):
            self.store.revoke(actor=self.second, reference=reference)
        self.assertEqual(self.store.resolve(reference).split(":", 1)[0], "900001")


if __name__ == "__main__":
    unittest.main()
