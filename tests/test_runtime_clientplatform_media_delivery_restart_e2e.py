from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from clientplatform.application.dispatch_worker import run_dispatch_batch
from clientplatform.domain.programs import DeliveryStatus, ProgressStatus
from clientplatform.infrastructure import (
    ConnectionRepository,
    DispatchOutboxRepository,
    TenancyRepository,
)
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.delivery_repository import DeliveryRepository
from clientplatform.infrastructure.program_repository import ProgramRepository
from clientplatform.runtime.media_gateway import (
    FilesystemMediaObjectStore,
    MediaGatewayConfig,
    start_media_gateway_runtime,
    stop_media_gateway_runtime,
)
from clientplatform.runtime.secrets import EnvironmentCredentialProvider
from clientplatform.transport import AdapterRegistry, TelegramDispatchAdapter
from clientplatform.transport.media import HmacMediaGatewayResolver
from clientplatform.transport.telegram_http import AiohttpTelegramBotClient
from services.db.schema import (
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_programs,
    clientplatform_tenancy,
)


class ClientPlatformPersistedMediaDeliveryRestartE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "clientplatform-staging.sqlite3"
        self.media_root = self.root / "objects"
        target = self.media_root / "private-bucket" / "programs" / "lesson-01.mp3"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"PERSISTED-clientplatform-AUDIO")
        self.provider = EnvironmentCredentialProvider(
            {
                "CLIENTPLATFORM_SECRET_TELEGRAM_MAIN": "telegram-restart-token",
                "CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY": "restart-signing-secret",
            }
        )
        self.enrollment_id = self._seed_database()
        self.gateway_config = MediaGatewayConfig(
            enabled=True,
            host="127.0.0.1",
            port=0,
            public_base_url="https://media.example.test/clientplatform",
            storage_mode="filesystem",
            allowed_buckets=frozenset({"private-bucket"}),
            filesystem_root=str(self.media_root),
            s3_endpoint="",
            s3_region="",
            s3_access_key_reference="secret://env/CLIENTPLATFORM_SECRET_S3_ACCESS_KEY",
            s3_secret_key_reference="secret://env/CLIENTPLATFORM_SECRET_S3_SECRET_KEY",
            s3_session_token_reference="",
            signing_secret_reference="secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY",
            max_object_bytes=1_000_000,
            upstream_timeout_seconds=10.0,
            chunk_size=8,
        )
        store = FilesystemMediaObjectStore(
            root=str(self.media_root),
            max_object_bytes=self.gateway_config.max_object_bytes,
        )
        self.gateway = await start_media_gateway_runtime(
            self.gateway_config,
            store=store,
            credential_provider=self.provider,
        )
        assert self.gateway is not None
        server = self.gateway.site._server
        assert server is not None and server.sockets
        self.gateway_port = int(server.sockets[0].getsockname()[1])
        self.provider_calls = 0
        self.provider_media: list[bytes] = []

    async def asyncTearDown(self) -> None:
        await stop_media_gateway_runtime()
        self.temp.cleanup()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _seed_database(self) -> str:
        conn = self._connect()
        try:
            clientplatform_tenancy.ensure(conn)
            clientplatform_customers.ensure(conn)
            clientplatform_programs.ensure(conn)
            clientplatform_connections.ensure(conn)
            tenancy = TenancyRepository(conn)
            customers = CustomerRepository(conn)
            programs = ProgramRepository(conn)
            deliveries = DeliveryRepository(conn)
            connections = ConnectionRepository(conn)
            outbox = DispatchOutboxRepository(conn)

            created = tenancy.create_business(
                owner_user_id=101,
                name="clientplatform staging practice",
            )
            owner = tenancy.resolve_context(
                user_id=101,
                business_id=created.business.id,
            )
            customer = customers.create_customer(
                actor=owner,
                display_name="Staging client",
            )
            identity = customers.attach_identity(
                actor=owner,
                customer_id=customer.id,
                platform="telegram",
                external_subject="700001",
            )
            program = programs.create_program(
                actor=owner,
                title="Persisted media program",
            )
            programs.add_lesson(
                actor=owner,
                program_id=program.id,
                title="Audio lesson",
                content_kind="audio",
                content_ref="s3://private-bucket/programs/lesson-01.mp3",
            )
            programs.publish_program(actor=owner, program_id=program.id)
            enrollment = deliveries.enroll_customer(
                actor=owner,
                program_id=program.id,
                customer_id=customer.id,
            )
            connection = connections.create_connection(
                actor=owner,
                platform="telegram",
                connection_type="telegram_shared_bot",
                external_account_id="clientplatform-staging-bot",
                credential_reference="secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_MAIN",
                permissions=("send_messages",),
            )
            connection = connections.activate_connection(
                actor=owner,
                connection_id=connection.id,
            )
            outbox.materialize(
                actor=owner,
                logical_delivery_id=enrollment.deliveries[0].id,
                connection_id=connection.id,
                customer_identity_id=identity.id,
            )
            conn.commit()
            return enrollment.enrollment.id
        finally:
            conn.close()

    def _local_gateway_url(self, signed_url: str) -> str:
        parsed = urlsplit(signed_url)
        return urlunsplit(
            (
                "http",
                f"127.0.0.1:{self.gateway_port}",
                parsed.path,
                parsed.query,
                "",
            )
        )

    def _patched_get_db(self):
        db_path = self.db_path

        @contextmanager
        def managed():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

        return managed

    async def _post_json(self, url, payload, timeout_seconds):
        del timeout_seconds
        self.provider_calls += 1
        self.assertIn("/bottelegram-restart-token/sendAudio", url)
        self.assertNotIn("s3://", payload["audio"])
        async with aiohttp.ClientSession() as session:
            async with session.get(self._local_gateway_url(payload["audio"])) as response:
                self.assertEqual(response.status, 200)
                self.provider_media.append(await response.read())
        return 200, {"ok": True, "result": {"message_id": 91001}}

    def _registry(self) -> AdapterRegistry:
        resolver = HmacMediaGatewayResolver(
            base_url=self.gateway_config.public_base_url,
            credential_provider=self.provider,
            signing_secret_reference=self.gateway_config.signing_secret_reference,
            ttl_seconds=120,
        )
        client = AiohttpTelegramBotClient(post_json=self._post_json)
        return AdapterRegistry(
            [TelegramDispatchAdapter(client, media_resolver=resolver)]
        )

    async def test_delivery_persists_across_restart_and_is_not_resent(self) -> None:
        with patch(
            "clientplatform.application.dispatch_worker.get_db",
            side_effect=self._patched_get_db(),
        ):
            first = await run_dispatch_batch(
                credential_provider=self.provider,
                adapters=self._registry(),
                limit=10,
            )

        self.assertEqual(
            (first.claimed, first.sent, first.retried, first.dead),
            (1, 1, 0, 0),
        )
        self.assertEqual(self.provider_calls, 1)
        self.assertEqual(self.provider_media, [b"PERSISTED-clientplatform-AUDIO"])

        restarted = self._connect()
        try:
            tenancy = TenancyRepository(restarted)
            business_id = str(
                restarted.execute("SELECT id FROM businesses LIMIT 1").fetchone()["id"]
            )
            owner = tenancy.resolve_context(user_id=101, business_id=business_id)
            record = DeliveryRepository(restarted).get_enrollment(
                actor=owner,
                enrollment_id=self.enrollment_id,
            )
            dispatch = restarted.execute(
                "SELECT status,provider_message_id FROM delivery_dispatch_outbox LIMIT 1"
            ).fetchone()
            self.assertEqual(dispatch["status"], "sent")
            self.assertEqual(dispatch["provider_message_id"], "91001")
            self.assertEqual(record.deliveries[0].status, DeliveryStatus.SENT)
            self.assertEqual(record.progress[0].status, ProgressStatus.DELIVERED)
        finally:
            restarted.close()

        with patch(
            "clientplatform.application.dispatch_worker.get_db",
            side_effect=self._patched_get_db(),
        ):
            second = await run_dispatch_batch(
                credential_provider=self.provider,
                adapters=self._registry(),
                limit=10,
            )

        self.assertEqual(
            (second.claimed, second.sent, second.retried, second.dead),
            (0, 0, 0, 0),
        )
        self.assertEqual(self.provider_calls, 1)


if __name__ == "__main__":
    unittest.main()
