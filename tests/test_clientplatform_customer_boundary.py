from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.customers import (
    CustomerIdentityConflict,
    CustomerIdentityStatus,
    CustomerNotFound,
    CustomerPlatform,
    CustomerStatus,
)
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.privacy_manifest import validate_clientplatform_privacy_manifest
from services.db.schema import (
    clientplatform_connections,
    clientplatform_customers,
    clientplatform_programs,
    clientplatform_tenancy,
)


class ClientPlatformCustomerBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_programs.ensure(self.conn)
        clientplatform_connections.ensure(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        self.customers = CustomerRepository(self.conn)
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

    def tearDown(self) -> None:
        self.conn.close()

    def test_same_telegram_person_isolated_between_businesses(self) -> None:
        customer_a = self.customers.create_customer(
            actor=self.owner_a,
            display_name="Общий человек",
        )
        customer_b = self.customers.create_customer(
            actor=self.owner_b,
            display_name="Тот же человек",
        )
        identity_a = self.customers.attach_identity(
            actor=self.owner_a,
            customer_id=customer_a.id,
            platform=CustomerPlatform.TELEGRAM,
            external_subject="777001",
        )
        identity_b = self.customers.attach_identity(
            actor=self.owner_b,
            customer_id=customer_b.id,
            platform=CustomerPlatform.TELEGRAM,
            external_subject="777001",
        )
        self.assertNotEqual(identity_a.id, identity_b.id)
        self.assertNotEqual(identity_a.customer_id, identity_b.customer_id)
        resolved_a = self.customers.find_by_identity(
            actor=self.owner_a,
            platform="telegram",
            external_subject="777001",
        )
        resolved_b = self.customers.find_by_identity(
            actor=self.owner_b,
            platform="telegram",
            external_subject="777001",
        )
        self.assertEqual(resolved_a.customer.id, customer_a.id)
        self.assertEqual(resolved_b.customer.id, customer_b.id)

    def test_active_identity_customer_list_is_platform_scoped_and_tenant_scoped(self) -> None:
        vk_customer = self.customers.create_customer(
            actor=self.owner_a,
            display_name="Клиент VK",
        )
        max_customer = self.customers.create_customer(
            actor=self.owner_a,
            display_name="Клиент MAX",
        )
        other_business_vk = self.customers.create_customer(
            actor=self.owner_b,
            display_name="Чужой VK",
        )
        self.customers.attach_identity(
            actor=self.owner_a,
            customer_id=vk_customer.id,
            platform="vk",
            external_subject="700001",
        )
        self.customers.attach_identity(
            actor=self.owner_a,
            customer_id=max_customer.id,
            platform="max",
            external_subject="800001",
        )
        self.customers.attach_identity(
            actor=self.owner_b,
            customer_id=other_business_vk.id,
            platform="vk",
            external_subject="700002",
        )

        vk = self.customers.list_customers_with_active_identity(
            actor=self.owner_a,
            platform=CustomerPlatform.VK,
        )
        max_items = self.customers.list_customers_with_active_identity(
            actor=self.owner_a,
            platform="max",
        )
        self.assertEqual([vk_customer.id], [item.id for item in vk])
        self.assertEqual([max_customer.id], [item.id for item in max_items])

    def test_cross_business_customer_lookup_is_denied(self) -> None:
        customer_b = self.customers.create_customer(
            actor=self.owner_b,
            display_name="Клиент Б",
        )
        with self.assertRaises(CustomerNotFound):
            self.customers.get_customer(
                actor=self.owner_a,
                customer_id=customer_b.id,
            )

    def test_database_rejects_cross_business_customer_author(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO customers(
                    id, business_id, display_name, status, created_by_member_id,
                    created_at, updated_at, archived_at
                ) VALUES(?, ?, ?, 'active', ?, ?, ?, NULL)
                """,
                (
                    "8c39cb79-c20d-4aae-a6f4-d288777f6ddb",
                    self.business_a.business.id,
                    "Подделка",
                    self.business_b.membership.id,
                    "2026-07-28T00:00:00+00:00",
                    "2026-07-28T00:00:00+00:00",
                ),
            )

    def test_database_rejects_cross_business_identity_link(self) -> None:
        customer_a = self.customers.create_customer(actor=self.owner_a)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO customer_identities(
                    id, business_id, customer_id, platform, external_subject,
                    username, display_name, status, created_at, updated_at, revoked_at
                ) VALUES(?, ?, ?, 'telegram', '9001', NULL, NULL, 'active', ?, ?, NULL)
                """,
                (
                    "b25ba861-1c01-457d-b880-dc49f4360d10",
                    self.business_b.business.id,
                    customer_a.id,
                    "2026-07-28T00:00:00+00:00",
                    "2026-07-28T00:00:00+00:00",
                ),
            )

    def test_identity_attach_is_idempotent_for_same_customer(self) -> None:
        customer = self.customers.create_customer(actor=self.owner_a)
        first = self.customers.attach_identity(
            actor=self.owner_a,
            customer_id=customer.id,
            platform="email",
            external_subject=" Person@Example.COM ",
            display_name="Первое имя",
        )
        second = self.customers.attach_identity(
            actor=self.owner_a,
            customer_id=customer.id,
            platform="email",
            external_subject="person@example.com",
            display_name="Новое имя",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.external_subject, "person@example.com")
        self.assertEqual(second.display_name, "Новое имя")

    def test_identity_conflict_does_not_silently_merge_customers(self) -> None:
        first = self.customers.create_customer(actor=self.owner_a)
        second = self.customers.create_customer(actor=self.owner_a)
        self.customers.attach_identity(
            actor=self.owner_a,
            customer_id=first.id,
            platform="telegram",
            external_subject="818181",
        )
        with self.assertRaises(CustomerIdentityConflict):
            self.customers.attach_identity(
                actor=self.owner_a,
                customer_id=second.id,
                platform="telegram",
                external_subject="818181",
            )

    def test_archive_revokes_routing_identity(self) -> None:
        customer = self.customers.create_customer(actor=self.owner_a)
        identity = self.customers.attach_identity(
            actor=self.owner_a,
            customer_id=customer.id,
            platform="phone",
            external_subject="+7 (999) 111-22-33",
        )
        self.assertEqual(identity.external_subject, "79991112233")
        archived = self.customers.archive_customer(
            actor=self.owner_a,
            customer_id=customer.id,
        )
        self.assertEqual(archived.status, CustomerStatus.ARCHIVED)
        record = self.customers.get_customer(
            actor=self.owner_a,
            customer_id=customer.id,
        )
        self.assertEqual(record.identities[0].status, CustomerIdentityStatus.REVOKED)
        with self.assertRaises(CustomerNotFound):
            self.customers.find_by_identity(
                actor=self.owner_a,
                platform="phone",
                external_subject="79991112233",
            )

    def test_content_and_marketing_roles_cannot_read_customer_pii(self) -> None:
        self.tenancy.grant_member(
            actor=self.owner_a,
            user_id=303,
            role=PlatformRole.MARKETER,
        )
        marketer = self.tenancy.resolve_context(
            user_id=303,
            business_id=self.business_a.business.id,
        )
        with self.assertRaises(TenantPermissionDenied):
            self.customers.list_customers(actor=marketer)
        with self.assertRaises(TenantPermissionDenied):
            self.customers.list_customers_with_active_identity(
                actor=marketer,
                platform="vk",
            )

    def test_support_role_can_manage_customer_record(self) -> None:
        self.tenancy.grant_member(
            actor=self.owner_a,
            user_id=404,
            role=PlatformRole.SUPPORT,
        )
        support = self.tenancy.resolve_context(
            user_id=404,
            business_id=self.business_a.business.id,
        )
        customer = self.customers.create_customer(
            actor=support,
            display_name="Клиент поддержки",
        )
        self.assertEqual(customer.created_by_member_id, support.membership_id)

    def test_clientplatform_privacy_manifest_fails_closed_for_unknown_business_table(self) -> None:
        report = validate_clientplatform_privacy_manifest(self.conn, strict=False)
        self.assertTrue(report.ok)
        self.assertEqual(
            set(report.discovered_business_tables),
            {
                "business_members",
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
        self.conn.execute(
            "CREATE TABLE unknown_tenant_data(id TEXT PRIMARY KEY, business_id TEXT NOT NULL)"
        )
        report = validate_clientplatform_privacy_manifest(self.conn, strict=False)
        self.assertIn("unknown_tenant_data", report.unknown_tables)
        with self.assertRaisesRegex(RuntimeError, "clientplatform_privacy_manifest_invalid"):
            validate_clientplatform_privacy_manifest(self.conn, strict=True)


if __name__ == "__main__":
    unittest.main()
