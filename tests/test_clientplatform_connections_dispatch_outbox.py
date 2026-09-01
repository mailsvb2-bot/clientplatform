from __future__ import annotations

import asyncio
import sqlite3
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from clientplatform.application.dispatch_worker import run_dispatch_batch
from clientplatform.domain.connections import (
    ClaimedDispatch,
    ConnectionInvariantViolation,
    ConnectionNotFound,
    ConnectionPlatform,
    ConnectionStatus,
    ConnectionType,
    DispatchInvariantViolation,
    DispatchLeaseLost,
    DispatchNotFound,
    DispatchStatus,
)
from clientplatform.domain.programs import ContentKind, DeliveryStatus, ProgressStatus
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from clientplatform.infrastructure import (
    ConnectionRepository,
    DispatchOutboxRepository,
    TenancyRepository,
)
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.delivery_repository import DeliveryRepository
from clientplatform.infrastructure.program_repository import ProgramRepository
from clientplatform.privacy_manifest import validate_clientplatform_privacy_manifest
from clientplatform.transport import AdapterRegistry, TelegramDispatchAdapter
from services.db.schema import (
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_programs,
    clientplatform_tenancy,
)


class ClientPlatformDispatchFixture:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_programs.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)

        self.tenancy = TenancyRepository(self.conn)
        self.customers = CustomerRepository(self.conn)
        self.programs = ProgramRepository(self.conn)
        self.deliveries = DeliveryRepository(self.conn)
        self.connections = ConnectionRepository(self.conn)
        self.outbox = DispatchOutboxRepository(self.conn)

        self.business_a = self.tenancy.create_business(
            owner_user_id=101,
            name="Практика Марии",
        )
        self.business_b = self.tenancy.create_business(
            owner_user_id=202,
            name="Школа Нины",
        )
        self.owner_a = self.tenancy.resolve_context(
            user_id=101,
            business_id=self.business_a.business.id,
        )
        self.owner_b = self.tenancy.resolve_context(
            user_id=202,
            business_id=self.business_b.business.id,
        )

        self.customer_a = self.customers.create_customer(
            actor=self.owner_a,
            display_name="Клиент Марии",
        )
        self.identity_a = self.customers.attach_identity(
            actor=self.owner_a,
            customer_id=self.customer_a.id,
            platform="telegram",
            external_subject="700001",
        )
        self.customer_b = self.customers.create_customer(
            actor=self.owner_b,
            display_name="Клиент Нины",
        )
        self.identity_b = self.customers.attach_identity(
            actor=self.owner_b,
            customer_id=self.customer_b.id,
            platform="telegram",
            external_subject="700002",
        )

        self.program_a = self.programs.create_program(
            actor=self.owner_a,
            title="Спокойный сон",
        )
        self.lesson_a = self.programs.add_lesson(
            actor=self.owner_a,
            program_id=self.program_a.id,
            title="Первое аудио",
            content_kind="audio",
            content_ref="s3://clientplatform/sleep-01.mp3",
        )
        self.programs.publish_program(
            actor=self.owner_a,
            program_id=self.program_a.id,
        )
        enrollment = self.deliveries.enroll_customer(
            actor=self.owner_a,
            program_id=self.program_a.id,
            customer_id=self.customer_a.id,
        )
        self.enrollment_a = enrollment.enrollment
        self.logical_delivery_a = enrollment.deliveries[0]

        self.connection_a = self.connections.create_connection(
            actor=self.owner_a,
            platform="telegram",
            connection_type="telegram_shared_bot",
            external_account_id="clientplatform-shared-bot",
            credential_reference="secret://clientplatform/business-a/telegram-token",
            permissions=("send_messages",),
        )
        self.connection_a = self.connections.activate_connection(
            actor=self.owner_a,
            connection_id=self.connection_a.id,
        )

    def materialize(self):
        return self.outbox.materialize(
            actor=self.owner_a,
            logical_delivery_id=self.logical_delivery_a.id,
            connection_id=self.connection_a.id,
            customer_identity_id=self.identity_a.id,
            now="2026-07-28T10:00:00+00:00",
        )

    def close(self) -> None:
        self.conn.close()


class ClientPlatformConnectionsDispatchOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = ClientPlatformDispatchFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_raw_tokens_and_platform_type_mismatch_are_rejected(self) -> None:
        with self.assertRaises(ConnectionInvariantViolation):
            self.fx.connections.create_connection(
                actor=self.fx.owner_a,
                platform="telegram",
                connection_type="telegram_shared_bot",
                external_account_id="raw-token-bot",
                credential_reference="123456:RAW_TOKEN",
            )
        with self.assertRaises(ConnectionInvariantViolation):
            self.fx.connections.create_connection(
                actor=self.fx.owner_a,
                platform="vk",
                connection_type="telegram_shared_bot",
                external_account_id="wrong-platform",
                credential_reference="secret://clientplatform/wrong-platform",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.fx.conn.execute(
                """
                INSERT INTO connections(
                    id,business_id,platform,connection_type,external_account_id,
                    credential_reference,permissions_json,status,
                    created_by_member_id,created_at,updated_at
                ) VALUES(?,?,'telegram','telegram_shared_bot','direct-raw',
                         '123456:RAW','[]','pending',?,?,?)
                """,
                (
                    "72f6fb34-a9b1-48c8-8798-1f27ef648663",
                    self.fx.business_a.business.id,
                    self.fx.business_a.membership.id,
                    "2026-07-28T10:00:00+00:00",
                    "2026-07-28T10:00:00+00:00",
                ),
            )

    def test_only_owner_or_administrator_manages_connections(self) -> None:
        self.fx.tenancy.grant_member(
            actor=self.fx.owner_a,
            user_id=303,
            role=PlatformRole.SUPPORT,
        )
        support = self.fx.tenancy.resolve_context(
            user_id=303,
            business_id=self.fx.business_a.business.id,
        )
        with self.assertRaises(TenantPermissionDenied):
            self.fx.connections.create_connection(
                actor=support,
                platform="telegram",
                connection_type="telegram_shared_bot",
                external_account_id="support-bot",
                credential_reference="secret://clientplatform/support-bot",
            )

    def test_duplicate_connection_is_idempotent(self) -> None:
        repeated = self.fx.connections.create_connection(
            actor=self.fx.owner_a,
            platform="telegram",
            connection_type="telegram_shared_bot",
            external_account_id="clientplatform-shared-bot",
            credential_reference="secret://clientplatform/business-a/telegram-token",
            permissions=("send_messages",),
        )
        self.assertEqual(repeated.id, self.fx.connection_a.id)
        self.assertEqual(len(self.fx.connections.list_connections(actor=self.fx.owner_a)), 1)

    def test_managed_bot_requires_managed_connection_and_secret_reference(self) -> None:
        with self.assertRaises(ConnectionNotFound):
            self.fx.connections.register_managed_bot(
                actor=self.fx.owner_a,
                connection_id=self.fx.connection_a.id,
                external_bot_id="111",
                webhook_secret_reference="secret://clientplatform/webhook/111",
            )
        managed_connection = self.fx.connections.create_connection(
            actor=self.fx.owner_a,
            platform="telegram",
            connection_type="telegram_managed_bot",
            external_account_id="managed-111",
            credential_reference="vault://clientplatform/managed/111/token",
        )
        with self.assertRaises(ConnectionInvariantViolation):
            self.fx.connections.register_managed_bot(
                actor=self.fx.owner_a,
                connection_id=managed_connection.id,
                external_bot_id="111",
                webhook_secret_reference="raw-webhook-secret",
            )
        bot = self.fx.connections.register_managed_bot(
            actor=self.fx.owner_a,
            connection_id=managed_connection.id,
            external_bot_id="111",
            webhook_secret_reference="secret://clientplatform/managed/111/webhook",
            username="maria_helper_bot",
        )
        self.assertEqual(bot.connection_id, managed_connection.id)
        self.assertEqual(bot.platform, ConnectionPlatform.TELEGRAM)

    def test_materialization_is_idempotent_and_contains_no_raw_secret(self) -> None:
        first = self.fx.materialize()
        second = self.fx.materialize()
        self.assertEqual(first.id, second.id)
        self.assertNotIn("token", first.payload_ref.lower())
        row = self.fx.conn.execute(
            "SELECT COUNT(*) AS c FROM delivery_dispatch_outbox"
        ).fetchone()
        self.assertEqual(int(row["c"]), 1)

    def test_identity_must_match_enrollment_customer_and_platform(self) -> None:
        other_customer = self.fx.customers.create_customer(
            actor=self.fx.owner_a,
            display_name="Другой клиент",
        )
        other_identity = self.fx.customers.attach_identity(
            actor=self.fx.owner_a,
            customer_id=other_customer.id,
            platform="telegram",
            external_subject="700003",
        )
        with self.assertRaises(DispatchInvariantViolation):
            self.fx.outbox.materialize(
                actor=self.fx.owner_a,
                logical_delivery_id=self.fx.logical_delivery_a.id,
                connection_id=self.fx.connection_a.id,
                customer_identity_id=other_identity.id,
            )

        vk_identity = self.fx.customers.attach_identity(
            actor=self.fx.owner_a,
            customer_id=self.fx.customer_a.id,
            platform="vk",
            external_subject="vk-700001",
        )
        with self.assertRaises(DispatchInvariantViolation):
            self.fx.outbox.materialize(
                actor=self.fx.owner_a,
                logical_delivery_id=self.fx.logical_delivery_a.id,
                connection_id=self.fx.connection_a.id,
                customer_identity_id=vk_identity.id,
            )

    def test_database_rejects_cross_platform_dispatch_link(self) -> None:
        vk_identity = self.fx.customers.attach_identity(
            actor=self.fx.owner_a,
            customer_id=self.fx.customer_a.id,
            platform="vk",
            external_subject="vk-700001",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.fx.conn.execute(
                """
                INSERT INTO delivery_dispatch_outbox(
                    id,business_id,platform,logical_delivery_id,connection_id,
                    customer_identity_id,payload_kind,payload_ref,idempotency_key,
                    status,attempts,available_at,created_at,updated_at
                ) VALUES(?,?,'telegram',?,?,?,'audio','s3://clientplatform/x.mp3',?,
                         'pending',0,?,?,?)
                """,
                (
                    "b47e63f4-c394-4bad-901f-7cc2f15ba82b",
                    self.fx.business_a.business.id,
                    self.fx.logical_delivery_a.id,
                    self.fx.connection_a.id,
                    vk_identity.id,
                    "cross-platform",
                    "2026-07-28T10:00:00+00:00",
                    "2026-07-28T10:00:00+00:00",
                    "2026-07-28T10:00:00+00:00",
                ),
            )

    def test_cross_business_cannot_materialize_or_read_dispatch(self) -> None:
        dispatch = self.fx.materialize()
        with self.assertRaises(DispatchNotFound):
            self.fx.outbox.materialize(
                actor=self.fx.owner_b,
                logical_delivery_id=self.fx.logical_delivery_a.id,
                connection_id=self.fx.connection_a.id,
                customer_identity_id=self.fx.identity_b.id,
            )
        with self.assertRaises(DispatchNotFound):
            self.fx.outbox.get_dispatch(
                actor=self.fx.owner_b,
                dispatch_id=dispatch.id,
            )

    def test_claim_is_exclusive_and_stale_lease_is_reclaimed(self) -> None:
        self.fx.materialize()
        start = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)
        first = self.fx.outbox.claim_due(
            limit=10,
            lock_ttl_seconds=60,
            now=start,
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].dispatch.status, DispatchStatus.SENDING)
        self.assertEqual(self.fx.outbox.claim_due(now=start), [])

        reclaimed = self.fx.outbox.claim_due(
            limit=10,
            lock_ttl_seconds=60,
            now=start + timedelta(seconds=61),
        )
        self.assertEqual(len(reclaimed), 1)
        self.assertNotEqual(
            first[0].dispatch.lock_token,
            reclaimed[0].dispatch.lock_token,
        )

    def test_transient_retry_keeps_connection_active_and_becomes_due(self) -> None:
        self.fx.materialize()
        start = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)
        item = self.fx.outbox.claim_due(now=start)[0]
        retry = self.fx.outbox.reschedule(
            item,
            error="temporary network",
            max_attempts=3,
            now=start,
        )
        self.assertEqual(retry.status, DispatchStatus.RETRY)
        connection = self.fx.connections.list_connections(actor=self.fx.owner_a)[0]
        self.assertEqual(connection.status, ConnectionStatus.ACTIVE)
        claimed_again = self.fx.outbox.claim_due(
            now=start + timedelta(seconds=6),
        )
        self.assertEqual(len(claimed_again), 1)

    def test_terminal_failure_dead_letters_and_marks_connection_attention(self) -> None:
        self.fx.materialize()
        start = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)
        item = self.fx.outbox.claim_due(now=start)[0]
        dead = self.fx.outbox.reschedule(
            item,
            error="permanent provider rejection",
            max_attempts=1,
            now=start,
        )
        self.assertEqual(dead.status, DispatchStatus.DEAD)
        connection = self.fx.connections.list_connections(actor=self.fx.owner_a)[0]
        self.assertEqual(connection.status, ConnectionStatus.ATTENTION)
        logical = self.fx.deliveries.get_enrollment(
            actor=self.fx.owner_a,
            enrollment_id=self.fx.enrollment_a.id,
        ).deliveries[0]
        self.assertEqual(logical.status, DeliveryStatus.FAILED)

    def test_mark_sent_updates_dispatch_logical_delivery_and_progress(self) -> None:
        dispatch = self.fx.materialize()
        item = self.fx.outbox.claim_due()[0]
        sent = self.fx.outbox.mark_sent(
            item,
            provider_message_id="tg-message-9001",
        )
        self.assertEqual(sent.id, dispatch.id)
        self.assertEqual(sent.status, DispatchStatus.SENT)
        self.assertEqual(sent.provider_message_id, "tg-message-9001")
        record = self.fx.deliveries.get_enrollment(
            actor=self.fx.owner_a,
            enrollment_id=self.fx.enrollment_a.id,
        )
        self.assertEqual(record.deliveries[0].status, DeliveryStatus.SENT)
        self.assertEqual(record.progress[0].status, ProgressStatus.DELIVERED)
        connection = self.fx.connections.list_connections(actor=self.fx.owner_a)[0]
        self.assertEqual(connection.status, ConnectionStatus.ACTIVE)
        self.assertIsNotNone(connection.last_success_at)

    def test_wrong_lease_token_cannot_settle_dispatch(self) -> None:
        self.fx.materialize()
        item = self.fx.outbox.claim_due()[0]
        forged = ClaimedDispatch(
            dispatch=replace(item.dispatch, lock_token="wrong-token"),
            external_subject=item.external_subject,
            credential_reference=item.credential_reference,
        )
        with self.assertRaises(DispatchLeaseLost):
            self.fx.outbox.mark_sent(
                forged,
                provider_message_id="forged",
            )
        row = self.fx.conn.execute(
            "SELECT status,lock_token FROM delivery_dispatch_outbox WHERE id=?",
            (item.dispatch.id,),
        ).fetchone()
        self.assertEqual(row["status"], "sending")
        self.assertEqual(row["lock_token"], item.dispatch.lock_token)

    def test_disabled_connection_is_not_claimable(self) -> None:
        self.fx.materialize()
        self.fx.connections.disable_connection(
            actor=self.fx.owner_a,
            connection_id=self.fx.connection_a.id,
        )
        self.assertEqual(self.fx.outbox.claim_due(), [])

    def test_privacy_manifest_covers_connection_and_outbox_tables(self) -> None:
        report = validate_clientplatform_privacy_manifest(self.fx.conn, strict=True)
        self.assertTrue(report.ok)
        self.assertEqual(
            set(report.discovered_business_tables),
            {
                "business_members",
                "clientplatform_owner_control_workspaces",
                "clientplatform_owner_onboarding_sessions",
                "clientplatform_owner_input_sessions",
                "connection_credentials",
                "connections",
                "customer_identities",
                "customers",
                "delivery_dispatch_outbox",
                "enrollments",
                "lesson_deliveries",
                "lesson_progress",
                "lessons",
                "managed_bot_credentials",
                "managed_bots",
                "programs",
            },
        )


class FakeTelegramClient:
    def __init__(self, *, fail_with_secret: bool = False, cancel: bool = False):
        self.calls: list[tuple[str, str, str, str]] = []
        self.fail_with_secret = fail_with_secret
        self.cancel = cancel

    async def _send(self, kind: str, *, token: str, chat_id: str, payload: str) -> str:
        self.calls.append((kind, token, chat_id, payload))
        if self.cancel:
            raise asyncio.CancelledError()
        if self.fail_with_secret:
            raise RuntimeError(f"provider rejected credential {token}")
        return f"telegram-{kind}-1"

    async def send_message(self, *, token: str, chat_id: str, text: str) -> str:
        return await self._send("message", token=token, chat_id=chat_id, payload=text)

    async def send_audio(self, *, token: str, chat_id: str, audio: str) -> str:
        return await self._send("audio", token=token, chat_id=chat_id, payload=audio)

    async def send_video(self, *, token: str, chat_id: str, video: str) -> str:
        return await self._send("video", token=token, chat_id=chat_id, payload=video)

    async def send_document(self, *, token: str, chat_id: str, document: str) -> str:
        return await self._send("document", token=token, chat_id=chat_id, payload=document)

    async def send_photo(self, *, token: str, chat_id: str, photo: str) -> str:
        return await self._send("photo", token=token, chat_id=chat_id, payload=photo)


class FakeCredentialProvider:
    def __init__(self, secret: str):
        self.secret = secret
        self.references: list[str] = []

    def resolve(self, reference: str) -> str:
        self.references.append(reference)
        return self.secret


class ClientPlatformDispatchWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fx = ClientPlatformDispatchFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def _patched_get_db(self):
        conn = self.fx.conn

        @contextmanager
        def managed():
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return managed

    async def test_telegram_adapter_routes_audio_and_uses_resolved_secret(self) -> None:
        self.fx.materialize()
        item = self.fx.outbox.claim_due()[0]
        client = FakeTelegramClient()
        adapter = TelegramDispatchAdapter(client)
        provider_id = await adapter.send(item, "REAL_TELEGRAM_TOKEN")
        self.assertEqual(provider_id, "telegram-audio-1")
        self.assertEqual(
            client.calls,
            [
                (
                    "audio",
                    "REAL_TELEGRAM_TOKEN",
                    "700001",
                    "s3://clientplatform/sleep-01.mp3",
                )
            ],
        )
        self.assertNotEqual(
            client.calls[0][1],
            item.credential_reference,
        )

    async def test_worker_resolves_secret_outside_payload_and_marks_sent(self) -> None:
        self.fx.materialize()
        client = FakeTelegramClient()
        credentials = FakeCredentialProvider("WORKER_SECRET_TOKEN")
        registry = AdapterRegistry([TelegramDispatchAdapter(client)])
        with patch(
            "clientplatform.application.dispatch_worker.get_db",
            side_effect=self._patched_get_db(),
        ):
            result = await run_dispatch_batch(
                credential_provider=credentials,
                adapters=registry,
                limit=10,
            )
        self.assertEqual((result.claimed, result.sent, result.retried, result.dead), (1, 1, 0, 0))
        self.assertEqual(
            credentials.references,
            ["secret://clientplatform/business-a/telegram-token"],
        )
        dump = "\n".join(
            str(tuple(row))
            for row in self.fx.conn.execute(
                "SELECT * FROM delivery_dispatch_outbox"
            ).fetchall()
        )
        self.assertNotIn("WORKER_SECRET_TOKEN", dump)

    async def test_worker_redacts_secret_before_persisting_retry_error(self) -> None:
        self.fx.materialize()
        secret = "VERY_SENSITIVE_TOKEN"
        client = FakeTelegramClient(fail_with_secret=True)
        credentials = FakeCredentialProvider(secret)
        registry = AdapterRegistry([TelegramDispatchAdapter(client)])
        with patch(
            "clientplatform.application.dispatch_worker.get_db",
            side_effect=self._patched_get_db(),
        ):
            result = await run_dispatch_batch(
                credential_provider=credentials,
                adapters=registry,
                max_attempts=3,
            )
        self.assertEqual((result.retried, result.dead), (1, 0))
        row = self.fx.conn.execute(
            "SELECT status,last_error FROM delivery_dispatch_outbox"
        ).fetchone()
        self.assertEqual(row["status"], "retry")
        self.assertIn("[redacted]", row["last_error"])
        self.assertNotIn(secret, row["last_error"])

    async def test_worker_cancellation_releases_claim_for_retry(self) -> None:
        self.fx.materialize()
        client = FakeTelegramClient(cancel=True)
        credentials = FakeCredentialProvider("WORKER_SECRET_TOKEN")
        registry = AdapterRegistry([TelegramDispatchAdapter(client)])
        with patch(
            "clientplatform.application.dispatch_worker.get_db",
            side_effect=self._patched_get_db(),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await run_dispatch_batch(
                    credential_provider=credentials,
                    adapters=registry,
                )
        row = self.fx.conn.execute(
            "SELECT status,lock_token,last_error FROM delivery_dispatch_outbox"
        ).fetchone()
        self.assertEqual(row["status"], "retry")
        self.assertIsNone(row["lock_token"])
        self.assertEqual(row["last_error"], "worker_cancelled")


if __name__ == "__main__":
    unittest.main()
