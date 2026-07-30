from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from clientplatform.application.program_media import ProgramMediaIngestPolicy
from clientplatform.domain.connections import ConnectionPlatform, ConnectionType
from clientplatform.domain.programs import ContentKind, EnrollmentStatus
from clientplatform.infrastructure import (
    ConnectionRepository,
    DispatchOutboxRepository,
    TenancyRepository,
)
from clientplatform.infrastructure.customer_progress_repository import (
    CustomerProgressRepository,
)
from clientplatform.infrastructure.delivery_repository import DeliveryRepository
from clientplatform.infrastructure.program_progress_repository import (
    ProgramProgressRepository,
)
from clientplatform.infrastructure.program_repository import ProgramRepository
from clientplatform.infrastructure.safe_bot_gateway_repository import (
    BotGatewayRepository,
)
from clientplatform.transport.telegram import TelegramDispatchAdapter
from handlers.clientplatform_program_media import materialize_program_content
from services.db.schema import (
    clientplatform_bot_gateway,
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_programs,
    clientplatform_tenancy,
)


class _ControlBot:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def get_file(self, file_id: str) -> Any:
        if file_id != "control-audio-id":
            raise AssertionError("unexpected control-bot file id")
        return SimpleNamespace(
            file_path="programs/lesson.mp3",
            file_size=len(self.payload),
        )

    async def download_file(
        self,
        file_path: str,
        *,
        destination: Path,
        timeout: float,
    ) -> None:
        if file_path != "programs/lesson.mp3" or timeout != 30.0:
            raise AssertionError("unexpected control-bot download request")
        destination.write_bytes(self.payload)


class _ControlMessage:
    def __init__(self, payload: bytes) -> None:
        self.text = None
        self.audio = SimpleNamespace(
            file_id="control-audio-id",
            file_size=len(payload),
            mime_type="audio/mpeg",
            file_name="lesson.mp3",
        )
        self.voice = None
        self.video = None
        self.document = None
        self.photo: list[Any] = []
        self.bot = _ControlBot(payload)


class _StoredMedia:
    def __init__(self) -> None:
        self.paths: list[Path] = []
        self.calls: list[dict[str, Any]] = []

    def put_file(self, path: Path, **kwargs: Any) -> Any:
        self.paths.append(path)
        self.calls.append(kwargs)
        if path.read_bytes() != b"first-vertical-audio":
            raise AssertionError("unexpected downloaded media")
        return SimpleNamespace(
            reference=(
                "s3://clientplatform-production/program-media/"
                "business/audio/aa/first-vertical.mp3"
            )
        )


class _MediaResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ContentKind]] = []

    async def resolve(self, reference: str, kind: ContentKind) -> str:
        self.calls.append((reference, kind))
        return "https://media.example.test/clientplatform/first-vertical.mp3"


class _TelegramClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def send_audio(
        self,
        *,
        token: str,
        chat_id: str,
        audio: str,
    ) -> str:
        self.calls.append(("audio", chat_id, audio))
        if token != "managed-bot-token":
            raise AssertionError("unexpected credential")
        return "provider-audio-1"

    async def send_message(
        self,
        *,
        token: str,
        chat_id: str,
        text: str,
    ) -> str:
        self.calls.append(("text", chat_id, text))
        if token != "managed-bot-token":
            raise AssertionError("unexpected credential")
        return "provider-text-2"


class ClientPlatformFirstVerticalE2E(unittest.IsolatedAsyncioTestCase):
    def _open(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        clientplatform_tenancy.ensure(conn)
        clientplatform_customers.ensure(conn)
        clientplatform_programs.ensure(conn)
        clientplatform_connections.ensure(conn)
        clientplatform_bot_gateway.ensure(conn)

    def _managed_route(
        self,
        conn: sqlite3.Connection,
        *,
        owner: Any,
        external_bot_id: str,
        suffix: str,
    ) -> tuple[Any, Any]:
        connections = ConnectionRepository(conn)
        connection = connections.create_connection(
            actor=owner,
            platform=ConnectionPlatform.TELEGRAM,
            connection_type=ConnectionType.TELEGRAM_MANAGED_BOT,
            external_account_id=external_bot_id,
            credential_reference=(
                f"secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_{suffix}"
            ),
            permissions=("send_message", "send_media"),
            now="2026-07-30T12:00:00+00:00",
        )
        connection = connections.activate_connection(
            actor=owner,
            connection_id=connection.id,
            now="2026-07-30T12:00:00+00:00",
        )
        managed = connections.register_managed_bot(
            actor=owner,
            connection_id=connection.id,
            external_bot_id=external_bot_id,
            webhook_secret_reference=(
                f"secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_{suffix}"
            ),
            username=f"clientplatform_{suffix.lower()}_bot",
            display_name=f"ClientPlatform {suffix}",
            now="2026-07-30T12:00:00+00:00",
        )
        route = BotGatewayRepository(conn).resolve_telegram_route(
            external_bot_id=managed.external_bot_id
        )
        return connection, route

    async def test_first_vertical_survives_restart_replay_and_second_business(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="clientplatform-first-vertical-") as raw:
            database = Path(raw) / "journey.sqlite3"
            conn = self._open(database)
            self._ensure_schema(conn)

            tenancy = TenancyRepository(conn)
            access_a = tenancy.create_business(
                owner_user_id=101,
                name="Практика Марии",
            )
            access_b = tenancy.create_business(
                owner_user_id=202,
                name="Школа Нины",
            )
            owner_a = tenancy.resolve_context(
                user_id=101,
                business_id=access_a.business.id,
            )
            owner_b = tenancy.resolve_context(
                user_id=202,
                business_id=access_b.business.id,
            )
            connection_a, route_a = self._managed_route(
                conn,
                owner=owner_a,
                external_bot_id="700001",
                suffix="MARIA",
            )
            _connection_b, route_b = self._managed_route(
                conn,
                owner=owner_b,
                external_bot_id="700002",
                suffix="NINA",
            )

            gateway = BotGatewayRepository(conn)
            customer_a = gateway.ensure_telegram_customer_link(
                route=route_a,
                telegram_user_id=5001,
                username="client",
                display_name="Анна Клиент",
                now="2026-07-30T12:01:00+00:00",
            )
            replay_a = gateway.ensure_telegram_customer_link(
                route=route_a,
                telegram_user_id=5001,
                username="client",
                display_name="Анна Клиент",
                now="2026-07-30T12:01:01+00:00",
            )
            customer_b = gateway.ensure_telegram_customer_link(
                route=route_b,
                telegram_user_id=5001,
                username="client",
                display_name="Анна Клиент",
                now="2026-07-30T12:01:02+00:00",
            )
            self.assertEqual(customer_a.customer_id, replay_a.customer_id)
            self.assertNotEqual(customer_a.customer_id, customer_b.customer_id)

            identity_a = conn.execute(
                """
                SELECT id FROM customer_identities
                WHERE business_id=? AND platform='telegram'
                  AND external_subject='5001' AND status='active'
                """,
                (owner_a.business_id,),
            ).fetchone()
            self.assertIsNotNone(identity_a)
            assert identity_a is not None

            stored_media = _StoredMedia()
            content_kind, content_ref = await materialize_program_content(
                _ControlMessage(b"first-vertical-audio"),
                business_id=owner_a.business_id,
                policy=ProgramMediaIngestPolicy(
                    enabled=True,
                    max_bytes=20_000_000,
                    timeout_seconds=30.0,
                ),
                store_media=stored_media.put_file,
            )
            self.assertEqual(content_kind, ContentKind.AUDIO)
            self.assertTrue(content_ref.startswith("s3://"))
            self.assertFalse(stored_media.paths[0].exists())

            programs = ProgramRepository(conn)
            program = programs.create_program(
                actor=owner_a,
                title="Первый сквозной курс",
                now="2026-07-30T12:02:00+00:00",
            )
            lessons = [
                programs.add_lesson(
                    actor=owner_a,
                    program_id=program.id,
                    title="Аудиоурок",
                    content_kind=content_kind,
                    content_ref=content_ref,
                    position=1,
                    now="2026-07-30T12:02:01+00:00",
                ),
                programs.add_lesson(
                    actor=owner_a,
                    program_id=program.id,
                    title="Итог",
                    content_kind=ContentKind.TEXT,
                    content_ref="Отметьте завершение программы",
                    position=2,
                    now="2026-07-30T12:02:02+00:00",
                ),
            ]
            programs.publish_program(
                actor=owner_a,
                program_id=program.id,
                now="2026-07-30T12:02:03+00:00",
            )
            enrollment = DeliveryRepository(conn).enroll_customer(
                actor=owner_a,
                program_id=program.id,
                customer_id=customer_a.customer_id,
                now="2026-07-30T12:03:00+00:00",
            )
            first_delivery = enrollment.deliveries[0]
            outbox = DispatchOutboxRepository(conn)
            outbox.materialize(
                actor=owner_a,
                logical_delivery_id=first_delivery.id,
                connection_id=connection_a.id,
                customer_identity_id=str(identity_a["id"]),
                now="2026-07-30T12:03:01+00:00",
            )
            conn.commit()

            resolver = _MediaResolver()
            telegram = _TelegramClient()
            adapter = TelegramDispatchAdapter(
                telegram,
                media_resolver=resolver,
            )
            claimed = outbox.claim_due(limit=10)
            self.assertEqual(len(claimed), 1)
            provider_id = await adapter.send(claimed[0], "managed-bot-token")
            outbox.mark_sent(
                claimed[0],
                provider_message_id=provider_id,
            )
            conn.commit()
            self.assertEqual(telegram.calls[0][0], "audio")
            self.assertEqual(telegram.calls[0][1], "5001")
            self.assertEqual(resolver.calls, [(content_ref, ContentKind.AUDIO)])

            conn.close()
            conn = self._open(database)
            outbox = DispatchOutboxRepository(conn)
            self.assertEqual(outbox.claim_due(limit=10), [])

            progress = CustomerProgressRepository(conn)
            first_completion = progress.complete_lesson(
                telegram_user_id=5001,
                business_id=owner_a.business_id,
                enrollment_id=enrollment.enrollment.id,
                lesson_position=1,
                now="2026-07-30T12:04:00+00:00",
            )
            repeated_completion = progress.complete_lesson(
                telegram_user_id=5001,
                business_id=owner_a.business_id,
                enrollment_id=enrollment.enrollment.id,
                lesson_position=1,
                now="2026-07-30T12:04:01+00:00",
            )
            self.assertTrue(first_completion.next_material_queued)
            self.assertFalse(repeated_completion.next_material_queued)

            second_claim = outbox.claim_due(limit=10)
            self.assertEqual(len(second_claim), 1)
            self.assertEqual(
                second_claim[0].dispatch.logical_delivery_id,
                first_completion.program.lessons[1].delivery_id,
            )
            second_provider_id = await adapter.send(
                second_claim[0],
                "managed-bot-token",
            )
            outbox.mark_sent(
                second_claim[0],
                provider_message_id=second_provider_id,
            )
            final = progress.complete_lesson(
                telegram_user_id=5001,
                business_id=owner_a.business_id,
                enrollment_id=enrollment.enrollment.id,
                lesson_position=2,
                now="2026-07-30T12:05:00+00:00",
            )
            conn.commit()

            self.assertEqual(final.program.summary.completed_lessons, 2)
            self.assertEqual(
                final.program.summary.enrollment_status,
                EnrollmentStatus.COMPLETED,
            )
            self.assertEqual(telegram.calls[1][0], "text")
            self.assertEqual(telegram.calls[1][2], "Отметьте завершение программы")

            progress_read = ProgramProgressRepository(conn)
            owner_a_view = progress_read.list_business_progress(actor=owner_a)
            owner_b_view = progress_read.list_business_progress(actor=owner_b)
            self.assertEqual(len(owner_a_view), 1)
            self.assertEqual(owner_a_view[0].customer_display_name, "Анна Клиент")
            self.assertEqual(owner_a_view[0].percent_complete, 100)
            self.assertEqual(owner_b_view, [])
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM delivery_dispatch_outbox "
                    "WHERE business_id=?",
                    (owner_a.business_id,),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                {lesson.id for lesson in lessons},
                {
                    item.lesson_id
                    for item in final.program.lessons
                },
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
