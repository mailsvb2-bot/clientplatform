from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from clientplatform.application.native_customer_interactions import (
    is_native_customer_interaction_input,
    process_native_customer_interaction,
)
from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from clientplatform.infrastructure import (
    ConnectionRepository,
    DispatchOutboxRepository,
    TenancyRepository,
)
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.delivery_repository import DeliveryRepository
from clientplatform.infrastructure.messenger_channel_repository import MessengerChannelRepository
from clientplatform.infrastructure.program_repository import ProgramRepository
from clientplatform.infrastructure.unified_dispatch_outbox import ClaimedProviderDispatch
from clientplatform.runtime.messenger_provider_clients import MaxRuntimeClient, VkRuntimeClient
from services.db.schema import create_or_update_tables


class NativeCustomerInteractionFixture:
    def __init__(self, *, platform: str = "vk") -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        create_or_update_tables(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        created = self.tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = self.tenancy.resolve_context(
            user_id=101, business_id=created.business.id
        )
        self.business_id = self.owner.business_id
        self.customers = CustomerRepository(self.conn)
        self.customer = self.customers.create_customer(
            actor=self.owner, display_name="Клиент"
        )
        self.identity = self.customers.attach_identity(
            actor=self.owner,
            customer_id=self.customer.id,
            platform=platform,
            external_subject="700001",
        )
        self.connections = ConnectionRepository(self.conn)
        connection_type = "vk_community" if platform == "vk" else "max_personal_bot"
        external_account_id = "441001" if platform == "vk" else "551001"
        connection = self.connections.create_connection(
            actor=self.owner,
            platform=platform,
            connection_type=connection_type,
            external_account_id=external_account_id,
            credential_reference=f"secret://env/CLIENTPLATFORM_SECRET_TEST_{platform.upper()}",
            permissions=("send_message", "send_media"),
        )
        self.connection = self.connections.activate_connection(
            actor=self.owner, connection_id=connection.id
        )
        self.routes = MessengerChannelRepository(self.conn)
        kwargs = dict(
            actor=self.owner,
            connection_id=self.connection.id,
            external_route_id=external_account_id,
            webhook_secret_reference=(
                f"secret://env/CLIENTPLATFORM_SECRET_TEST_{platform.upper()}_WEBHOOK"
            ),
        )
        if platform == "vk":
            kwargs["confirmation_code_reference"] = (
                "secret://env/CLIENTPLATFORM_SECRET_TEST_VK_CONFIRMATION"
            )
        self.route = self.routes.register_route(**kwargs)
        self.outbox = DispatchOutboxRepository(self.conn)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def db(self):
        yield self.conn

    def latest_interaction(self) -> tuple[dict[str, object], sqlite3.Row]:
        row = self.conn.execute(
            "SELECT * FROM provider_dispatch_outbox "
            "WHERE source_kind='customer_interaction' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        return json.loads(str(row["payload_ref"])), row


class NativeCustomerInteractionOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = NativeCustomerInteractionFixture(platform="vk")

    def tearDown(self) -> None:
        self.fx.close()

    def test_interaction_materialization_is_idempotent_and_tenant_scoped(self) -> None:
        interaction = CustomerInteractionMessage(
            text="Меню",
            rows=((CustomerInteractionButton("Программы", "cpi:programs:0"),),),
        )
        first = self.fx.outbox.materialize_customer_interaction(
            business_id=self.fx.business_id,
            connection_id=self.fx.connection.id,
            customer_identity_id=self.fx.identity.id,
            customer_id=self.fx.customer.id,
            platform="vk",
            interaction=interaction,
            interaction_key="route:event-1:customer-ui-v1",
        )
        second = self.fx.outbox.materialize_customer_interaction(
            business_id=self.fx.business_id,
            connection_id=self.fx.connection.id,
            customer_identity_id=self.fx.identity.id,
            customer_id=self.fx.customer.id,
            platform="vk",
            interaction=interaction,
            interaction_key="route:event-1:customer-ui-v1",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.source_kind, "customer_interaction")
        self.assertEqual(first.payload_kind.value, "mixed")
        self.assertEqual(CustomerInteractionMessage.from_json(first.payload_ref), interaction)

        other = self.fx.tenancy.create_business(owner_user_id=202, name="Чужой бизнес")
        with self.assertRaisesRegex(ValueError, "tenant-scoped"):
            self.fx.outbox.materialize_customer_interaction(
                business_id=other.business.id,
                connection_id=self.fx.connection.id,
                customer_identity_id=self.fx.identity.id,
                customer_id=self.fx.customer.id,
                platform="vk",
                interaction=interaction,
                interaction_key="cross-tenant",
            )

    def test_regular_text_is_not_mistaken_for_product_ui_command(self) -> None:
        self.assertFalse(is_native_customer_interaction_input("Хочу обсудить стоимость"))
        self.assertTrue(is_native_customer_interaction_input("Мои программы"))
        self.assertTrue(is_native_customer_interaction_input("cpi:slots:0"))


class NativeCustomerInteractionJourneyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = NativeCustomerInteractionFixture(platform="vk")
        self.programs = ProgramRepository(self.fx.conn)
        program = self.programs.create_program(actor=self.fx.owner, title="Курс спокойствия")
        self.lesson1 = self.programs.add_lesson(
            actor=self.fx.owner,
            program_id=program.id,
            title="Первый шаг",
            content_kind="text",
            content_ref="Материал 1",
        )
        self.lesson2 = self.programs.add_lesson(
            actor=self.fx.owner,
            program_id=program.id,
            title="Второй шаг",
            content_kind="text",
            content_ref="Материал 2",
        )
        self.program = self.programs.publish_program(
            actor=self.fx.owner, program_id=program.id
        )
        enrollment = DeliveryRepository(self.fx.conn).enroll_customer(
            actor=self.fx.owner,
            program_id=self.program.id,
            customer_id=self.fx.customer.id,
        )
        self.enrollment = enrollment.enrollment
        first_delivery = enrollment.deliveries[0]
        self.fx.outbox.materialize(
            actor=self.fx.owner,
            logical_delivery_id=first_delivery.id,
            connection_id=self.fx.connection.id,
            customer_identity_id=self.fx.identity.id,
        )
        claimed = self.fx.outbox.claim_due(limit=1)
        self.assertEqual(len(claimed), 1)
        self.fx.outbox.mark_sent(claimed[0], provider_message_id="vk-first")

        activity = ActivityRepository(self.fx.conn)
        activity.upsert_profile(
            actor=self.fx.owner,
            activity_description="Консультации",
            timezone_name="Europe/Moscow",
        )
        capability = activity.enable_capability(
            actor=self.fx.owner, connector_key="consultations"
        )
        offering = activity.create_offering(
            actor=self.fx.owner,
            capability_id=capability.id,
            title="Консультация",
            description="60 минут",
        )
        self.slot = BookingRepository(self.fx.conn).create_slot(
            actor=self.fx.owner,
            offering_id=offering.id,
            local_start="31.08.2026 15:00",
            duration_minutes=60,
            now="2026-08-21T00:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.fx.close()

    def _process(self, text: str, event: str) -> dict[str, object]:
        with patch(
            "clientplatform.application.native_customer_interactions.get_db",
            self.fx.db,
        ):
            handled = process_native_customer_interaction(
                route=self.fx.route,
                identity=self.fx.identity,
                raw_text=text,
                provider_event_id=event,
            )
        self.assertTrue(handled)
        payload, _row = self.fx.latest_interaction()
        return payload

    def test_program_progress_next_material_and_booking_share_canonical_state(self) -> None:
        programs = self._process("Мои программы", "event-programs")
        commands = [
            button["command"]
            for row in programs["rows"]
            for button in row
        ]
        self.assertIn(f"cpi:program:{self.enrollment.id}:0", commands)

        detail = self._process(
            f"cpi:program:{self.enrollment.id}:0", "event-program-detail"
        )
        detail_commands = [
            button["command"] for row in detail["rows"] for button in row
        ]
        done = next(command for command in detail_commands if command.startswith("cpi:done:"))
        completed = self._process(done, "event-done")
        self.assertIn("Следующий материал", str(completed["text"]))
        progress = self.fx.conn.execute(
            "SELECT status FROM lesson_progress WHERE business_id=? "
            "AND enrollment_id=? AND lesson_id=?",
            (self.fx.business_id, self.enrollment.id, self.lesson1.id),
        ).fetchone()
        self.assertEqual(progress["status"], "completed")
        next_dispatch = self.fx.conn.execute(
            """
            SELECT o.platform,o.customer_identity_id,o.status
            FROM delivery_dispatch_outbox o
            JOIN lesson_deliveries d
              ON d.id=o.logical_delivery_id AND d.business_id=o.business_id
            WHERE d.business_id=? AND d.enrollment_id=? AND d.lesson_id=?
            LIMIT 1
            """,
            (self.fx.business_id, self.enrollment.id, self.lesson2.id),
        ).fetchone()
        self.assertIsNotNone(next_dispatch)
        self.assertEqual(next_dispatch["platform"], "vk")
        self.assertEqual(next_dispatch["customer_identity_id"], self.fx.identity.id)

        slots = self._process("Доступная запись", "event-slots")
        slot_commands = [
            button["command"] for row in slots["rows"] for button in row
        ]
        book = next(command for command in slot_commands if command.startswith("cpi:book:"))
        booked = self._process(book, "event-book")
        self.assertIn("Запись подтверждена", str(booked["text"]))
        slot = self.fx.conn.execute(
            "SELECT status,booked_customer_id FROM booking_slots WHERE id=?",
            (self.slot.slot.id,),
        ).fetchone()
        self.assertEqual(slot["status"], "booked")
        self.assertEqual(slot["booked_customer_id"], self.fx.customer.id)
        outcome = self.fx.conn.execute(
            "SELECT outcome_type,customer_id FROM business_outcome_events "
            "WHERE business_id=? AND subject_ref=?",
            (self.fx.business_id, f"booking_slot:{self.slot.slot.id}"),
        ).fetchone()
        self.assertEqual(outcome["outcome_type"], "booking_created")
        self.assertEqual(outcome["customer_id"], self.fx.customer.id)

    def test_same_provider_event_does_not_duplicate_interaction(self) -> None:
        self._process("Меню", "same-event")
        self._process("Меню", "same-event")
        count = self.fx.conn.execute(
            "SELECT COUNT(*) FROM provider_dispatch_outbox "
            "WHERE source_kind='customer_interaction'"
        ).fetchone()[0]
        self.assertEqual(count, 1)


class NativeCustomerInteractionMaxSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = NativeCustomerInteractionFixture(platform="max")

    def tearDown(self) -> None:
        self.fx.close()

    def test_max_interaction_provider_boundary_is_never_automatically_replayed(self) -> None:
        interaction = CustomerInteractionMessage(text="Меню")
        self.fx.outbox.materialize_customer_interaction(
            business_id=self.fx.business_id,
            connection_id=self.fx.connection.id,
            customer_identity_id=self.fx.identity.id,
            customer_id=self.fx.customer.id,
            platform="max",
            interaction=interaction,
            interaction_key="max-event",
            now="2026-08-21T00:00:00+00:00",
        )
        claimed = self.fx.outbox.claim_due(
            limit=1,
            lock_ttl_seconds=60,
            now=datetime(2026, 8, 21, 0, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(claimed), 1)
        self.assertIsInstance(claimed[0], ClaimedProviderDispatch)
        self.assertTrue(self.fx.outbox.mark_provider_non_replay_boundary(claimed[0]))

        replay = self.fx.outbox.claim_due(
            limit=1,
            lock_ttl_seconds=60,
            now=datetime(2026, 8, 21, 0, 2, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(replay, [])
        row = self.fx.conn.execute(
            "SELECT status,last_error FROM provider_dispatch_outbox "
            "WHERE source_kind='customer_interaction'"
        ).fetchone()
        self.assertEqual(row["status"], "dead")
        self.assertIn("ambiguous", row["last_error"])


class NativeCustomerInteractionProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_vk_interaction_uses_callback_keyboard_and_deterministic_random_id(self) -> None:
        sender = AsyncMock()
        sender.send_text.return_value = 9001
        interaction = CustomerInteractionMessage(
            text="Меню",
            rows=((CustomerInteractionButton("Программы", "cpi:programs:0"),),),
        )
        with patch(
            "clientplatform.runtime.messenger_provider_clients._vk_sender",
            return_value=sender,
        ):
            result = await VkRuntimeClient().send_interaction(
                token="provider-token",
                external_subject="700001",
                interaction=interaction,
                idempotency_key="same-key",
            )
        self.assertEqual(result, "9001")
        kwargs = sender.send_text.await_args.kwargs
        keyboard = json.loads(kwargs["keyboard_json"])
        payload = json.loads(keyboard["buttons"][0][0]["action"]["payload"])
        self.assertEqual(payload["command"], "cpi:programs:0")
        self.assertGreater(int(kwargs["random_id"]), 0)

    async def test_max_interaction_uses_native_inline_keyboard_without_legacy_ui(self) -> None:
        sender = AsyncMock()
        sender.send_text.return_value = {"message_id": "max-1"}
        interaction = CustomerInteractionMessage(
            text="Меню",
            rows=((CustomerInteractionButton("Запись", "cpi:slots:0"),),),
        )
        with patch(
            "clientplatform.runtime.messenger_provider_clients._max_sender",
            return_value=sender,
        ):
            result = await MaxRuntimeClient().send_interaction(
                token="provider-token",
                external_subject="700001",
                interaction=interaction,
                idempotency_key="ignored-by-provider",
            )
        self.assertEqual(result, "max-1")
        kwargs = sender.send_text.await_args.kwargs
        self.assertFalse(kwargs["legacy_ui"])
        button = kwargs["attachments"][0]["payload"]["buttons"][0][0]
        self.assertEqual(button["type"], "callback")
        self.assertEqual(button["payload"], "cpi:slots:0")


if __name__ == "__main__":
    unittest.main()
