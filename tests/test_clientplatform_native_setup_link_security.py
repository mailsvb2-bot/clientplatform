from __future__ import annotations

import hashlib
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.infrastructure.native_messenger_setup_repository import (
    NativeMessengerSetupRejected,
    NativeMessengerSetupRepository,
)
from clientplatform.runtime import native_messenger_setup_links as setup_links


class _CredentialProvider:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.references: list[str] = []

    def resolve(self, reference: str) -> str:
        self.references.append(reference)
        return self.secret


class NativeSetupLinkSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE businesses(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_by_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE business_members(
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revoked_at TEXT NULL
            );
            CREATE TABLE messenger_connection_setup_sessions(
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                token_digest TEXT NOT NULL,
                created_by_member_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT NULL
            );
            """
        )
        now = "2026-08-21T07:00:00+00:00"
        self.business_id = str(uuid4())
        self.other_business_id = str(uuid4())
        self.member_id = str(uuid4())
        self.other_member_id = str(uuid4())
        self.user_id = 1001
        self.conn.execute(
            "INSERT INTO businesses VALUES(?,?,?,?,?,?)",
            (self.business_id, "Alpha", "active", self.user_id, now, now),
        )
        self.conn.execute(
            "INSERT INTO business_members VALUES(?,?,?,?,?,?,?,NULL)",
            (
                self.member_id,
                self.business_id,
                self.user_id,
                PlatformRole.OWNER.value,
                "active",
                now,
                now,
            ),
        )
        self.conn.execute(
            "INSERT INTO businesses VALUES(?,?,?,?,?,?)",
            (self.other_business_id, "Beta", "active", 2002, now, now),
        )
        self.conn.execute(
            "INSERT INTO business_members VALUES(?,?,?,?,?,?,?,NULL)",
            (
                self.other_member_id,
                self.other_business_id,
                2002,
                PlatformRole.OWNER.value,
                "active",
                now,
                now,
            ),
        )
        self.actor = TenantContext(
            membership_id=self.member_id,
            business_id=self.business_id,
            user_id=self.user_id,
            role=PlatformRole.OWNER,
        )
        self.secret = "s" * 48
        self.provider = _CredentialProvider(self.secret)

    def tearDown(self) -> None:
        self.conn.close()

    def _service(self) -> setup_links.NativeMessengerSetupLinkService:
        return setup_links.NativeMessengerSetupLinkService(
            credential_provider=self.provider,
            public_base_url="https://client.example.test",
            signing_secret_reference="secret://env/TEST_SIGNING_SECRET",
        )

    def _issue_command(self) -> str:
        with patch.object(setup_links, "get_db", return_value=self.conn):
            return self._service().issue_command(
                actor=self.actor,
                platform=ConnectionPlatform.VK,
                ttl_seconds=600,
            )

    def test_hmac_token_is_deterministic_and_domain_bound_to_session_and_expiry(self) -> None:
        session_a = str(uuid4())
        session_b = str(uuid4())
        expiry_a = "2026-08-21T07:10:00+00:00"
        expiry_b = "2026-08-21T07:11:00+00:00"

        first = setup_links.derive_native_setup_token(
            signing_secret=self.secret,
            session_id=session_a,
            expires_at=expiry_a,
        )
        repeated = setup_links.derive_native_setup_token(
            signing_secret=self.secret,
            session_id=session_a,
            expires_at=expiry_a,
        )
        other_session = setup_links.derive_native_setup_token(
            signing_secret=self.secret,
            session_id=session_b,
            expires_at=expiry_a,
        )
        other_expiry = setup_links.derive_native_setup_token(
            signing_secret=self.secret,
            session_id=session_a,
            expires_at=expiry_b,
        )

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, other_session)
        self.assertNotEqual(first, other_expiry)
        self.assertNotIn("clientplatform", first)

    def test_durable_setup_command_contains_only_non_secret_session_uuid(self) -> None:
        command = self._issue_command()
        session_id = setup_links.parse_native_setup_command(command)

        self.assertIsNotNone(session_id)
        self.assertEqual(command, f"cpm:setup:{session_id}")
        self.assertNotIn("https://", command)
        self.assertNotIn("/clientplatform/connect/", command)

        row = self.conn.execute(
            "SELECT token_digest FROM messenger_connection_setup_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertRegex(str(row["token_digest"]), r"^[0-9a-f]{64}$")
        self.assertNotIn(str(row["token_digest"]), command)

    def test_resolve_materializes_https_url_only_for_exact_business(self) -> None:
        command = self._issue_command()

        with patch.object(setup_links, "get_db_ro", return_value=self.conn):
            url = self._service().resolve_command_url(
                command=command,
                business_id=self.business_id,
            )

        self.assertIsNotNone(url)
        self.assertTrue(str(url).startswith("https://client.example.test/clientplatform/connect/"))
        token = str(url).rsplit("/", 1)[-1]
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        stored = self.conn.execute(
            "SELECT token_digest FROM messenger_connection_setup_sessions"
        ).fetchone()[0]
        self.assertEqual(stored, digest)

        with patch.object(setup_links, "get_db_ro", return_value=self.conn):
            with self.assertRaises(setup_links.NativeMessengerSetupLinkRejected):
                self._service().resolve_command_url(
                    command=command,
                    business_id=self.other_business_id,
                )

    def test_consumed_session_cannot_be_materialized_again(self) -> None:
        command = self._issue_command()
        session_id = setup_links.parse_native_setup_command(command)
        self.conn.execute(
            "UPDATE messenger_connection_setup_sessions SET consumed_at=? WHERE id=?",
            ("2026-08-21T07:01:00+00:00", session_id),
        )

        with patch.object(setup_links, "get_db_ro", return_value=self.conn):
            with self.assertRaises(setup_links.NativeMessengerSetupLinkRejected):
                self._service().resolve_command_url(
                    command=command,
                    business_id=self.business_id,
                )

    def test_expired_session_is_rejected_by_reference_lookup(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        session_id = str(uuid4())
        expires = (now - timedelta(seconds=1)).isoformat(timespec="seconds")
        token = setup_links.derive_native_setup_token(
            signing_secret=self.secret,
            session_id=session_id,
            expires_at=expires,
        )
        repo = NativeMessengerSetupRepository(self.conn)
        repo.issue(
            actor=self.actor,
            platform=ConnectionPlatform.MAX,
            ttl_seconds=60,
            now=now - timedelta(seconds=61),
            session_id=session_id,
            token=token,
        )

        with self.assertRaises(NativeMessengerSetupRejected):
            repo.inspect_reference(
                session_id=session_id,
                business_id=self.business_id,
                now=now,
            )

    def test_revoked_creator_membership_revokes_setup_link(self) -> None:
        command = self._issue_command()
        self.conn.execute(
            "UPDATE business_members SET status='revoked' WHERE id=?",
            (self.member_id,),
        )

        with patch.object(setup_links, "get_db_ro", return_value=self.conn):
            with self.assertRaises(setup_links.NativeMessengerSetupLinkRejected):
                self._service().resolve_command_url(
                    command=command,
                    business_id=self.business_id,
                )

    def test_tampered_stored_digest_is_rejected(self) -> None:
        command = self._issue_command()
        session_id = setup_links.parse_native_setup_command(command)
        self.conn.execute(
            "UPDATE messenger_connection_setup_sessions SET token_digest=? WHERE id=?",
            ("0" * 64, session_id),
        )

        with patch.object(setup_links, "get_db_ro", return_value=self.conn):
            with self.assertRaisesRegex(
                setup_links.NativeMessengerSetupLinkRejected,
                "digest",
            ):
                self._service().resolve_command_url(
                    command=command,
                    business_id=self.business_id,
                )

    def test_non_https_public_base_and_short_secret_fail_closed(self) -> None:
        insecure = setup_links.NativeMessengerSetupLinkService(
            credential_provider=self.provider,
            public_base_url="http://client.example.test",
            signing_secret_reference="secret://env/TEST_SIGNING_SECRET",
        )
        with self.assertRaisesRegex(
            setup_links.NativeMessengerSetupLinkRejected,
            "HTTPS",
        ):
            insecure.issue_command(actor=self.actor, platform=ConnectionPlatform.VK)

        short_secret = setup_links.NativeMessengerSetupLinkService(
            credential_provider=_CredentialProvider("short"),
            public_base_url="https://client.example.test",
            signing_secret_reference="secret://env/TEST_SIGNING_SECRET",
        )
        with self.assertRaisesRegex(
            setup_links.NativeMessengerSetupLinkRejected,
            "32 bytes",
        ):
            short_secret.issue_command(actor=self.actor, platform=ConnectionPlatform.VK)


if __name__ == "__main__":
    unittest.main()
