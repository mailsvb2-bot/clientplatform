from __future__ import annotations

import asyncio
import sqlite3
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from clientplatform.domain.connections import (
    ClaimedDispatch,
    ConnectionPlatform,
    Dispatch,
    DispatchStatus,
)
from clientplatform.domain.customers import CustomerPlatform
from clientplatform.domain.messenger_channels import (
    CustomerChannelIdentityConflict,
    CustomerChannelLinkRejected,
    CustomerIngressContext,
)
from clientplatform.domain.programs import ContentKind
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.infrastructure.messenger_channel_repository import MessengerChannelRepository
from clientplatform.transport.native_messenger import MaxDispatchAdapter, VkDispatchAdapter
from services.db.schema import (
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_messenger_channels,
)


class OmnichannelIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
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
                revoked_at TEXT,
                UNIQUE(id, business_id),
                FOREIGN KEY(business_id) REFERENCES businesses(id)
            );
            """
        )
        clientplatform_customers.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        clientplatform_messenger_channels.ensure(self.conn)
        now = "2026-08-16T00:00:00+00:00"
        self.business_a = str(uuid4())
        self.business_b = str(uuid4())
        self.member_a = str(uuid4())
        self.member_b = str(uuid4())
        self.conn.executemany(
            "INSERT INTO businesses(id,name,status,created_by_user_id,created_at,updated_at) VALUES(?,?,'active',?,?,?)",
            (
                (self.business_a, "A", 101, now, now),
                (self.business_b, "B", 202, now, now),
            ),
        )
        self.conn.executemany(
            """
            INSERT INTO business_members(
                id,business_id,user_id,role,status,created_at,updated_at,revoked_at
            ) VALUES(?,?,?,'owner','active',?,?,NULL)
            """,
            (
                (self.member_a, self.business_a, 101, now, now),
                (self.member_b, self.business_b, 202, now, now),
            ),
        )
        self.vk_connection = self._insert_connection(
            business_id=self.business_a,
            member_id=self.member_a,
            platform="vk",
            connection_type="vk_community",
            external_account_id="vk-group-42",
        )
        self.max_connection = self._insert_connection(
            business_id=self.business_a,
            member_id=self.member_a,
            platform="max",
            connection_type="max_personal_bot",
            external_account_id="max-bot-77",
        )
        self.other_max_connection = self._insert_connection(
            business_id=self.business_b,
            member_id=self.member_b,
            platform="max",
            connection_type="max_personal_bot",
            external_account_id="max-bot-other",
        )
        self.conn.commit()
        self.repo = MessengerChannelRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_connection(
        self,
        *,
        business_id: str,
        member_id: str,
        platform: str,
        connection_type: str,
        external_account_id: str,
    ) -> str:
        connection_id = str(uuid4())
        now = "2026-08-16T00:00:00+00:00"
        self.conn.execute(
            """
            INSERT INTO connections(
                id,business_id,platform,connection_type,external_account_id,
                credential_reference,permissions_json,status,created_by_member_id,
                created_at,updated_at,last_success_at,last_error_at,last_error_code
            ) VALUES(?,?,?,?,?,'secret://env/TEST_PROVIDER_TOKEN','[]','active',?,?,?,NULL,NULL,NULL)
            """,
            (
                connection_id,
                business_id,
                platform,
                connection_type,
                external_account_id,
                member_id,
                now,
                now,
            ),
        )
        return connection_id

    def _actor(self, business_id: str, member_id: str, user_id: int) -> TenantContext:
        return TenantContext(
            business_id=business_id,
            user_id=user_id,
            membership_id=member_id,
            role=PlatformRole.OWNER,
        )

    def test_first_seen_vk_customer_is_idempotent_and_tenant_scoped(self) -> None:
        context_a = CustomerIngressContext(
            business_id=self.business_a,
            connection_id=self.vk_connection,
            platform=CustomerPlatform.VK,
        )
        first = self.repo.ensure_customer_identity(
            context=context_a,
            external_subject="12345",
            username="vk-user",
            display_name="VK User",
        )
        replay = self.repo.ensure_customer_identity(
            context=context_a,
            external_subject="12345",
        )
        self.assertEqual(first.id, replay.id)
        self.assertEqual(first.customer_id, replay.customer_id)

        other_context = CustomerIngressContext(
            business_id=self.business_b,
            connection_id=self.other_max_connection,
            platform=CustomerPlatform.MAX,
        )
        other = self.repo.ensure_customer_identity(
            context=other_context,
            external_subject="12345",
        )
        self.assertNotEqual(first.customer_id, other.customer_id)
        self.assertEqual(self.business_a, first.business_id)
        self.assertEqual(self.business_b, other.business_id)

    def test_link_token_binds_max_to_same_customer_and_is_single_use(self) -> None:
        vk_context = CustomerIngressContext(
            business_id=self.business_a,
            connection_id=self.vk_connection,
            platform=CustomerPlatform.VK,
        )
        vk_identity = self.repo.ensure_customer_identity(
            context=vk_context,
            external_subject="vk-1",
        )
        issued = self.repo.issue_customer_link(
            actor=self._actor(self.business_a, self.member_a, 101),
            customer_id=vk_identity.customer_id,
            target_platform=CustomerPlatform.MAX,
            ttl_seconds=600,
            now=datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc),
        )
        row = self.conn.execute(
            "SELECT token_digest FROM customer_channel_link_tokens WHERE business_id=?",
            (self.business_a,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(64, len(str(row["token_digest"])))
        self.assertNotEqual(issued.token, row["token_digest"])

        max_context = CustomerIngressContext(
            business_id=self.business_a,
            connection_id=self.max_connection,
            platform=CustomerPlatform.MAX,
        )
        linked = self.repo.consume_customer_link(
            context=max_context,
            token=issued.token,
            external_subject="max-99",
            now=datetime(2026, 8, 16, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(vk_identity.customer_id, linked.customer_id)
        identities = self.conn.execute(
            "SELECT platform,customer_id FROM customer_identities WHERE business_id=? ORDER BY platform",
            (self.business_a,),
        ).fetchall()
        self.assertEqual({"vk", "max"}, {str(row["platform"]) for row in identities})
        self.assertEqual({vk_identity.customer_id}, {str(row["customer_id"]) for row in identities})
        with self.assertRaises(CustomerChannelLinkRejected):
            self.repo.consume_customer_link(
                context=max_context,
                token=issued.token,
                external_subject="max-99",
                now=datetime(2026, 8, 16, 0, 2, tzinfo=timezone.utc),
            )

    def test_link_token_cannot_cross_business_or_target_platform(self) -> None:
        vk_context = CustomerIngressContext(
            business_id=self.business_a,
            connection_id=self.vk_connection,
            platform=CustomerPlatform.VK,
        )
        identity = self.repo.ensure_customer_identity(
            context=vk_context,
            external_subject="vk-cross",
        )
        issued = self.repo.issue_customer_link(
            actor=self._actor(self.business_a, self.member_a, 101),
            customer_id=identity.customer_id,
            target_platform=CustomerPlatform.MAX,
            now=datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc),
        )
        with self.assertRaises(CustomerChannelLinkRejected):
            self.repo.consume_customer_link(
                context=vk_context,
                token=issued.token,
                external_subject="vk-second",
                now=datetime(2026, 8, 16, 0, 1, tzinfo=timezone.utc),
            )
        with self.assertRaises(CustomerChannelLinkRejected):
            self.repo.consume_customer_link(
                context=CustomerIngressContext(
                    business_id=self.business_b,
                    connection_id=self.other_max_connection,
                    platform=CustomerPlatform.MAX,
                ),
                token=issued.token,
                external_subject="max-other",
                now=datetime(2026, 8, 16, 0, 1, tzinfo=timezone.utc),
            )

    def test_existing_foreign_identity_is_never_silently_merged(self) -> None:
        max_context = CustomerIngressContext(
            business_id=self.business_a,
            connection_id=self.max_connection,
            platform=CustomerPlatform.MAX,
        )
        first = self.repo.ensure_customer_identity(
            context=max_context,
            external_subject="max-owned",
        )
        vk_identity = self.repo.ensure_customer_identity(
            context=CustomerIngressContext(
                business_id=self.business_a,
                connection_id=self.vk_connection,
                platform=CustomerPlatform.VK,
            ),
            external_subject="vk-other-customer",
        )
        self.assertNotEqual(first.customer_id, vk_identity.customer_id)
        issued = self.repo.issue_customer_link(
            actor=self._actor(self.business_a, self.member_a, 101),
            customer_id=vk_identity.customer_id,
            target_platform=CustomerPlatform.MAX,
            now=datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc),
        )
        with self.assertRaises(CustomerChannelIdentityConflict):
            self.repo.consume_customer_link(
                context=max_context,
                token=issued.token,
                external_subject="max-owned",
                now=datetime(2026, 8, 16, 0, 1, tzinfo=timezone.utc),
            )
        row = self.conn.execute(
            "SELECT consumed_at FROM customer_channel_link_tokens WHERE token_digest!=? OR business_id=? ORDER BY created_at DESC LIMIT 1",
            ("x" * 64, self.business_a),
        ).fetchone()
        self.assertIsNone(row["consumed_at"])

    def test_route_is_bound_to_active_connection_and_exact_external_account(self) -> None:
        route = self.repo.register_route(
            actor=self._actor(self.business_a, self.member_a, 101),
            connection_id=self.vk_connection,
            external_route_id="vk-group-42",
            webhook_secret_reference="secret://env/VK_WEBHOOK_SECRET_A",
        )
        resolved = self.repo.resolve_route(
            route_id=route.id,
            expected_platform=ConnectionPlatform.VK,
        )
        self.assertEqual(self.business_a, resolved.business_id)
        self.assertEqual(self.vk_connection, resolved.connection_id)
        with self.assertRaises(CustomerChannelLinkRejected):
            self.repo.register_route(
                actor=self._actor(self.business_a, self.member_a, 101),
                connection_id=self.max_connection,
                external_route_id="wrong-max-bot",
                webhook_secret_reference="secret://env/MAX_WEBHOOK_SECRET_A",
            )


class _FakeNativeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def send_text(self, *, token, external_subject, text, idempotency_key):
        self.calls.append(("text", token, external_subject, idempotency_key))
        return "provider-text-1"

    async def send_media(self, *, token, external_subject, kind, media, idempotency_key):
        self.calls.append((kind.value, token, external_subject, media))
        return "provider-media-1"


class _FakeResolver:
    async def resolve(self, reference: str, kind: ContentKind) -> str:
        return f"https://media.example/{kind.value}/{reference}"


class OmnichannelAdapterTests(unittest.TestCase):
    def _claim(self, platform: ConnectionPlatform, kind: ContentKind, payload: str) -> ClaimedDispatch:
        now = "2026-08-16T00:00:00+00:00"
        return ClaimedDispatch(
            dispatch=Dispatch(
                id=str(uuid4()),
                business_id=str(uuid4()),
                platform=platform,
                logical_delivery_id=str(uuid4()),
                connection_id=str(uuid4()),
                customer_identity_id=str(uuid4()),
                payload_kind=kind,
                payload_ref=payload,
                idempotency_key=f"delivery:{platform.value}:{kind.value}:1",
                status=DispatchStatus.SENDING,
                attempts=1,
                available_at=now,
                created_at=now,
                updated_at=now,
                locked_at=now,
                lock_token=str(uuid4()),
            ),
            external_subject="external-77",
            credential_reference="secret://env/PROVIDER_TOKEN",
        )

    def test_vk_and_max_text_share_same_dispatch_contract(self) -> None:
        for adapter_type, platform in (
            (VkDispatchAdapter, ConnectionPlatform.VK),
            (MaxDispatchAdapter, ConnectionPlatform.MAX),
        ):
            with self.subTest(platform=platform):
                client = _FakeNativeClient()
                adapter = adapter_type(client, media_resolver=_FakeResolver())
                result = asyncio.run(
                    adapter.send(
                        self._claim(platform, ContentKind.TEXT, "hello"),
                        "secret-token",
                    )
                )
                self.assertEqual("provider-text-1", result)
                self.assertEqual("text", client.calls[0][0])

    def test_media_uses_shared_resolver_and_video_remains_explicit_media_kind(self) -> None:
        client = _FakeNativeClient()
        adapter = VkDispatchAdapter(client, media_resolver=_FakeResolver())
        result = asyncio.run(
            adapter.send(
                self._claim(ConnectionPlatform.VK, ContentKind.VIDEO, "s3://bucket/video.mp4"),
                "secret-token",
            )
        )
        self.assertEqual("provider-media-1", result)
        self.assertEqual("video", client.calls[0][0])
        self.assertTrue(client.calls[0][3].startswith("https://media.example/video/"))

    def test_adapter_rejects_wrong_platform_before_provider_call(self) -> None:
        client = _FakeNativeClient()
        adapter = VkDispatchAdapter(client)
        with self.assertRaises(ValueError):
            asyncio.run(
                adapter.send(
                    self._claim(ConnectionPlatform.MAX, ContentKind.TEXT, "hello"),
                    "secret-token",
                )
            )
        self.assertEqual([], client.calls)


if __name__ == "__main__":
    unittest.main()
