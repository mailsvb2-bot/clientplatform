from __future__ import annotations

import os
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from clientplatform.application.control import (
    business_connection_statuses,
    create_single_lesson_program,
    prepare_native_program_delivery,
    prepare_program_delivery,
)
from clientplatform.domain.activity import (
    ACTIVITY_CONNECTORS,
    ActivityInvariantViolation,
    ActivityNotFound,
    CapabilityKind,
    CapabilityStatus,
)
from clientplatform.infrastructure import ConnectionRepository, TenancyRepository
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.privacy_manifest import validate_clientplatform_privacy_manifest
from clientplatform.runtime.control_bot import (
    CONTROL_BOT_CREDENTIAL_ENV,
    bind_control_bot_secret,
    control_bot_enabled,
)
from clientplatform.runtime.dispatch_runtime import dispatch_runtime_config
from services.db.schema import (
    clientplatform_activity,
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_programs,
    clientplatform_tenancy,
)


class ClientPlatformActivityOnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_programs.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        self.activity = ActivityRepository(self.conn)
        self.business_a = self.tenancy.create_business(owner_user_id=101, name="Практика Марии")
        self.business_b = self.tenancy.create_business(owner_user_id=202, name="Автосервис Жоры")
        self.owner_a = self.tenancy.resolve_context(
            user_id=101,
            business_id=self.business_a.business.id,
        )
        self.owner_b = self.tenancy.resolve_context(
            user_id=202,
            business_id=self.business_b.business.id,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_free_form_activity_and_multiple_connectors(self) -> None:
        profile = self.activity.upsert_profile(
            actor=self.owner_a,
            activity_description=(
                "  Консультирую родителей по вопросам сна детей и создаю аудио-программы  "
            ),
            timezone_name="Europe/Moscow",
        )
        self.assertEqual(
            profile.activity_description,
            "Консультирую родителей по вопросам сна детей и создаю аудио-программы",
        )
        programs = self.activity.enable_capability(
            actor=self.owner_a,
            connector_key="programs",
        )
        consultations = self.activity.enable_capability(
            actor=self.owner_a,
            connector_key="consultations",
        )
        custom = self.activity.enable_capability(
            actor=self.owner_a,
            connector_key="custom",
            title="Разбор семейного режима",
        )
        ready = self.activity.complete_profile(actor=self.owner_a)

        self.assertEqual(ready.status.value, "ready")
        self.assertEqual(programs.kind, CapabilityKind.PROGRAMS)
        self.assertEqual(consultations.kind, CapabilityKind.CONSULTATIONS)
        self.assertEqual(custom.title, "Разбор семейного режима")
        self.assertEqual(
            {item.connector_key for item in self.activity.list_capabilities(actor=self.owner_a)},
            {"programs", "consultations", "custom"},
        )

    def test_connector_registry_is_an_extension_boundary(self) -> None:
        self.assertEqual(
            tuple(ACTIVITY_CONNECTORS),
            ("programs", "consultations", "services", "custom"),
        )
        self.assertFalse(ACTIVITY_CONNECTORS["programs"].supports_offerings)
        self.assertTrue(ACTIVITY_CONNECTORS["consultations"].supports_offerings)
        self.assertTrue(ACTIVITY_CONNECTORS["services"].supports_offerings)
        self.assertTrue(ACTIVITY_CONNECTORS["custom"].supports_offerings)

    def test_profile_requires_at_least_one_enabled_capability(self) -> None:
        self.activity.upsert_profile(
            actor=self.owner_a,
            activity_description="Консультирую предпринимателей",
            timezone_name="Europe/Moscow",
        )
        with self.assertRaises(ActivityInvariantViolation):
            self.activity.complete_profile(actor=self.owner_a)

    def test_consultation_and_service_offerings_use_generic_model(self) -> None:
        self.activity.upsert_profile(
            actor=self.owner_a,
            activity_description="Провожу консультации и обучение",
            timezone_name="Europe/Moscow",
        )
        consultations = self.activity.enable_capability(
            actor=self.owner_a,
            connector_key="consultations",
        )
        programs = self.activity.enable_capability(
            actor=self.owner_a,
            connector_key="programs",
        )
        offering = self.activity.create_offering(
            actor=self.owner_a,
            capability_id=consultations.id,
            title="Первая консультация",
            description="60 минут, разбор ситуации и письменный план следующих шагов",
        )
        self.assertEqual(offering.title, "Первая консультация")
        with self.assertRaises(ActivityInvariantViolation):
            self.activity.create_offering(
                actor=self.owner_a,
                capability_id=programs.id,
                title="Неверная запись",
                description="Программы используют специализированную модель уроков",
            )

    def test_activity_objects_are_invisible_across_businesses(self) -> None:
        self.activity.upsert_profile(
            actor=self.owner_a,
            activity_description="Консультирую родителей",
            timezone_name="Europe/Moscow",
        )
        capability = self.activity.enable_capability(
            actor=self.owner_a,
            connector_key="consultations",
        )
        with self.assertRaises(ActivityNotFound):
            self.activity.get_capability(actor=self.owner_b, capability_id=capability.id)

    def test_invite_persists_only_hash_and_claims_one_telegram_customer(self) -> None:
        issued = self.activity.issue_customer_invite(
            actor=self.owner_a,
            now="2026-07-28T12:00:00+00:00",
        )
        stored = self.conn.execute(
            "SELECT token_hash, status FROM customer_invites WHERE id=?",
            (issued.invite.id,),
        ).fetchone()
        self.assertNotEqual(stored["token_hash"], issued.token)
        self.assertNotIn(issued.token, stored["token_hash"])
        self.assertEqual(stored["status"], "active")

        claim = self.activity.claim_customer_invite(
            token=issued.token,
            telegram_user_id=700001,
            username="client_one",
            display_name="Первый клиент",
            now="2026-07-28T12:10:00+00:00",
        )
        repeated = self.activity.claim_customer_invite(
            token=issued.token,
            telegram_user_id=700001,
            username="client_one",
            display_name="Первый клиент",
            now="2026-07-28T12:11:00+00:00",
        )
        self.assertEqual(claim.customer_id, repeated.customer_id)
        self.assertTrue(repeated.already_connected)
        with self.assertRaisesRegex(ActivityInvariantViolation, "already been used"):
            self.activity.claim_customer_invite(
                token=issued.token,
                telegram_user_id=700002,
                username="client_two",
                display_name="Второй клиент",
                now="2026-07-28T12:12:00+00:00",
            )

        customer = CustomerRepository(self.conn).find_by_identity(
            actor=self.owner_a,
            platform="telegram",
            external_subject="700001",
        )
        self.assertEqual(customer.customer.id, claim.customer_id)

    def test_control_orchestration_creates_program_and_real_dispatch(self) -> None:
        customers = CustomerRepository(self.conn)
        customer = customers.create_customer(
            actor=self.owner_a,
            display_name="Клиент Марии",
        )
        customers.attach_identity(
            actor=self.owner_a,
            customer_id=customer.id,
            platform="telegram",
            external_subject="700001",
        )

        @contextmanager
        def local_db():
            yield self.conn

        with patch("clientplatform.application.control.get_db", local_db):
            program = create_single_lesson_program(
                actor=self.owner_a,
                program_title="Спокойный сон",
                lesson_title="Первое аудио",
                content_kind="audio",
                content_ref="telegram-file-id",
            )
            prepared = prepare_program_delivery(
                actor=self.owner_a,
                program_id=program.program.id,
                customer_id=customer.id,
                bot_id=900001,
            )

        self.assertEqual(prepared.program.program.title, "Спокойный сон")
        self.assertEqual(prepared.connection.status.value, "active")
        self.assertEqual(
            prepared.connection.credential_reference,
            "secret://env/CLIENTPLATFORM_SECRET_CONTROL_TELEGRAM_BOT_TOKEN",
        )
        self.assertEqual(prepared.dispatch.payload_ref, "telegram-file-id")
        self.assertEqual(prepared.dispatch.status.value, "pending")

    def test_operational_staff_reads_only_tenant_connection_statuses(self) -> None:
        connections = ConnectionRepository(self.conn)
        connection = connections.create_connection(
            actor=self.owner_a,
            platform="vk",
            connection_type="vk_community",
            external_account_id="441002",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_TEST_VK_STATUS",
        )
        self.tenancy.grant_member(
            actor=self.owner_a,
            user_id=303,
            role="support",
        )
        support = self.tenancy.resolve_context(
            user_id=303,
            business_id=self.business_a.business.id,
        )

        @contextmanager
        def local_db():
            yield self.conn

        with patch("clientplatform.application.control.get_db", local_db):
            statuses = business_connection_statuses(actor=support)

        self.assertEqual(
            statuses,
            [(connection.platform, connection.status)],
        )

    def test_program_delivery_prefers_most_recent_active_vk_channel(self) -> None:
        customers = CustomerRepository(self.conn)
        customer = customers.create_customer(actor=self.owner_a, display_name="Клиент VK")
        customers.attach_identity(
            actor=self.owner_a, customer_id=customer.id, platform="telegram",
            external_subject="700010", now="2026-08-20T10:00:00+00:00",
        )
        vk_identity = customers.attach_identity(
            actor=self.owner_a, customer_id=customer.id, platform="vk",
            external_subject="880010", now="2026-08-20T11:00:00+00:00",
        )
        self.conn.execute(
            "UPDATE customer_identities SET last_contact_at=? WHERE id=?",
            ("2026-08-20T11:30:00+00:00", vk_identity.id),
        )
        connections = ConnectionRepository(self.conn)
        vk = connections.create_connection(
            actor=self.owner_a, platform="vk", connection_type="vk_community",
            external_account_id="441001",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_TEST_VK",
            permissions=("send_message", "send_media"),
        )
        vk = connections.activate_connection(actor=self.owner_a, connection_id=vk.id)

        @contextmanager
        def local_db():
            yield self.conn

        with patch("clientplatform.application.control.get_db", local_db):
            program = create_single_lesson_program(
                actor=self.owner_a, program_title="VK программа", lesson_title="Шаг",
                content_kind="text", content_ref="Привет VK",
            )
            prepared = prepare_program_delivery(
                actor=self.owner_a, program_id=program.program.id,
                customer_id=customer.id, bot_id=900001,
            )

        self.assertEqual(prepared.connection.id, vk.id)
        self.assertEqual(prepared.connection.platform.value, "vk")
        self.assertEqual(prepared.dispatch.customer_identity_id, vk_identity.id)
        self.assertEqual(prepared.dispatch.platform.value, "vk")

    def test_program_delivery_uses_max_without_telegram_identity(self) -> None:
        customers = CustomerRepository(self.conn)
        customer = customers.create_customer(actor=self.owner_a, display_name="Клиент MAX")
        max_identity = customers.attach_identity(
            actor=self.owner_a, customer_id=customer.id, platform="max",
            external_subject="990010", now="2026-08-20T12:00:00+00:00",
        )
        connections = ConnectionRepository(self.conn)
        max_connection = connections.create_connection(
            actor=self.owner_a, platform="max", connection_type="max_personal_bot",
            external_account_id="551001",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_TEST_MAX",
            permissions=("send_message", "send_media"),
        )
        max_connection = connections.activate_connection(
            actor=self.owner_a, connection_id=max_connection.id
        )

        @contextmanager
        def local_db():
            yield self.conn

        with patch("clientplatform.application.control.get_db", local_db):
            program = create_single_lesson_program(
                actor=self.owner_a, program_title="MAX программа", lesson_title="Шаг",
                content_kind="text", content_ref="Привет MAX",
            )
            prepared = prepare_program_delivery(
                actor=self.owner_a, program_id=program.program.id,
                customer_id=customer.id, bot_id=900001,
            )

        self.assertEqual(prepared.connection.id, max_connection.id)
        self.assertEqual(prepared.dispatch.customer_identity_id, max_identity.id)
        self.assertEqual(prepared.dispatch.platform.value, "max")

    def test_native_program_delivery_uses_exact_requested_platform(self) -> None:
        customers = CustomerRepository(self.conn)
        customer = customers.create_customer(actor=self.owner_a, display_name="VK и MAX")
        vk_identity = customers.attach_identity(
            actor=self.owner_a,
            customer_id=customer.id,
            platform="vk",
            external_subject="880001",
            now="2026-08-20T10:00:00+00:00",
        )
        customers.attach_identity(
            actor=self.owner_a,
            customer_id=customer.id,
            platform="max",
            external_subject="990001",
            now="2026-08-20T12:00:00+00:00",
        )
        connections = ConnectionRepository(self.conn)
        vk = connections.create_connection(
            actor=self.owner_a,
            platform="vk",
            connection_type="vk_community",
            external_account_id="441201",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_TEST_NATIVE_VK",
            permissions=("send_message",),
        )
        vk = connections.activate_connection(actor=self.owner_a, connection_id=vk.id)
        max_connection = connections.create_connection(
            actor=self.owner_a,
            platform="max",
            connection_type="max_personal_bot",
            external_account_id="551201",
            credential_reference="secret://env/CLIENTPLATFORM_SECRET_TEST_NATIVE_MAX",
            permissions=("send_message",),
        )
        connections.activate_connection(
            actor=self.owner_a, connection_id=max_connection.id
        )

        @contextmanager
        def local_db():
            yield self.conn

        with patch("clientplatform.application.control.get_db", local_db):
            program = create_single_lesson_program(
                actor=self.owner_a,
                program_title="Точный VK маршрут",
                lesson_title="Шаг",
                content_kind="text",
                content_ref="VK",
            )
            prepared = prepare_native_program_delivery(
                actor=self.owner_a,
                program_id=program.program.id,
                customer_id=customer.id,
                platform="vk",
            )

        self.assertEqual(vk.id, prepared.connection.id)
        self.assertEqual("vk", prepared.dispatch.platform.value)
        self.assertEqual(vk_identity.id, prepared.dispatch.customer_identity_id)

    def test_native_program_delivery_rejects_non_native_platform_before_enrollment(self) -> None:
        customers = CustomerRepository(self.conn)
        customer = customers.create_customer(actor=self.owner_a, display_name="Telegram")

        @contextmanager
        def local_db():
            yield self.conn

        with patch("clientplatform.application.control.get_db", local_db):
            program = create_single_lesson_program(
                actor=self.owner_a,
                program_title="Не native",
                lesson_title="Шаг",
                content_kind="text",
                content_ref="test",
            )
            with self.assertRaisesRegex(ValueError, "requires VK or MAX"):
                prepare_native_program_delivery(
                    actor=self.owner_a,
                    program_id=program.program.id,
                    customer_id=customer.id,
                    platform="telegram",
                )

        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM enrollments WHERE business_id=? AND customer_id=?",
            (self.owner_a.business_id, customer.id),
        ).fetchone()
        self.assertEqual(0, int(row["c"]))

    def test_program_delivery_fails_before_enrollment_on_ambiguous_native_connection(self) -> None:
        customers = CustomerRepository(self.conn)
        customer = customers.create_customer(actor=self.owner_a, display_name="Два VK")
        customers.attach_identity(
            actor=self.owner_a, customer_id=customer.id, platform="vk",
            external_subject="880099", now="2026-08-20T13:00:00+00:00",
        )
        connections = ConnectionRepository(self.conn)
        for external_id in ("441099", "441100"):
            created = connections.create_connection(
                actor=self.owner_a, platform="vk", connection_type="vk_community",
                external_account_id=external_id,
                credential_reference=(
                    "secret://env/CLIENTPLATFORM_SECRET_TEST_VK_" + external_id
                ),
                permissions=("send_message",),
            )
            connections.activate_connection(actor=self.owner_a, connection_id=created.id)

        @contextmanager
        def local_db():
            yield self.conn

        with patch("clientplatform.application.control.get_db", local_db):
            program = create_single_lesson_program(
                actor=self.owner_a, program_title="Не угадывать канал", lesson_title="Шаг",
                content_kind="text", content_ref="test",
            )
            with self.assertRaisesRegex(ValueError, "multiple active vk connections"):
                prepare_program_delivery(
                    actor=self.owner_a, program_id=program.program.id,
                    customer_id=customer.id, bot_id=900001,
                )

        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM enrollments WHERE business_id=? AND customer_id=?",
            (self.owner_a.business_id, customer.id),
        ).fetchone()
        self.assertEqual(int(row["c"]), 0)

    def test_privacy_manifest_covers_every_new_business_table(self) -> None:
        report = validate_clientplatform_privacy_manifest(self.conn, require_complete=False)
        self.assertTrue(report.ok)
        for table in (
            "business_profiles",
            "business_capabilities",
            "business_offerings",
            "customer_invites",
        ):
            self.assertIn(table, report.discovered_business_tables)


class ClientPlatformControlRuntimeTests(unittest.TestCase):
    def test_control_mode_explicitly_enables_dispatch_default(self) -> None:
        with patch.dict(os.environ, {"CLIENTPLATFORM_CONTROL_BOT_ENABLED": "1"}, clear=False):
            os.environ.pop("CLIENTPLATFORM_DISPATCH_RUNTIME_ENABLED", None)
            self.assertTrue(control_bot_enabled())
            self.assertTrue(dispatch_runtime_config().enabled)

    def test_control_token_is_mirrored_only_inside_secret_namespace(self) -> None:
        with patch.dict(os.environ, {"CLIENTPLATFORM_CONTROL_BOT_ENABLED": "1"}, clear=False):
            os.environ.pop(CONTROL_BOT_CREDENTIAL_ENV, None)
            bind_control_bot_secret("123456:private-token")
            self.assertEqual(os.environ[CONTROL_BOT_CREDENTIAL_ENV], "123456:private-token")


if __name__ == "__main__":
    unittest.main()
