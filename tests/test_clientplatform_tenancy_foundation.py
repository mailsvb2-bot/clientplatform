from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.tenancy import (
    MembershipStatus,
    OwnerOnboardingStep,
    PlatformRole,
    TenantAccessDenied,
    TenantInvariantViolation,
    TenantPermissionDenied,
)
from clientplatform.infrastructure import TenancyRepository
from services.db.schema import clientplatform_tenancy


class ClientPlatformTenancyFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        self.repo = TenancyRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_schema_has_explicit_business_scope(self) -> None:
        business_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(businesses)").fetchall()
        }
        member_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(business_members)").fetchall()
        }
        self.assertIn("id", business_columns)
        self.assertIn("business_id", member_columns)
        self.assertIn("user_id", member_columns)
        self.assertIn("role", member_columns)
        self.assertIn("status", member_columns)

    def test_create_business_atomically_creates_owner_membership(self) -> None:
        access = self.repo.create_business(owner_user_id=101, name="  Мария   Практика  ")
        self.assertEqual(access.business.name, "Мария Практика")
        self.assertEqual(access.membership.user_id, 101)
        self.assertEqual(access.membership.role, PlatformRole.OWNER)
        self.assertEqual(access.membership.status, MembershipStatus.ACTIVE)
        context = self.repo.resolve_context(user_id=101, business_id=access.business.id)
        self.assertEqual(context.business_id, access.business.id)

    def test_same_user_can_have_multiple_explicit_business_contexts(self) -> None:
        first = self.repo.create_business(owner_user_id=101, name="Практика")
        second = self.repo.create_business(owner_user_id=101, name="Школа")
        contexts = [
            self.repo.resolve_context(user_id=101, business_id=first.business.id),
            self.repo.resolve_context(user_id=101, business_id=second.business.id),
        ]
        self.assertNotEqual(contexts[0].business_id, contexts[1].business_id)
        with self.assertRaises(TenantAccessDenied):
            contexts[0].assert_business(second.business.id)
        accesses = self.repo.list_accessible_businesses(user_id=101)
        self.assertEqual({item.business.name for item in accesses}, {"Практика", "Школа"})

    def test_cross_business_membership_is_never_inferred_from_user_id(self) -> None:
        business_a = self.repo.create_business(owner_user_id=101, name="Бизнес А")
        business_b = self.repo.create_business(owner_user_id=202, name="Бизнес Б")
        owner_a = self.repo.resolve_context(user_id=101, business_id=business_a.business.id)
        self.repo.grant_member(actor=owner_a, user_id=303, role=PlatformRole.MANAGER)

        allowed = self.repo.resolve_context(user_id=303, business_id=business_a.business.id)
        self.assertEqual(allowed.role, PlatformRole.MANAGER)
        with self.assertRaises(TenantAccessDenied):
            self.repo.resolve_context(user_id=303, business_id=business_b.business.id)

    def test_revocation_invalidates_access_immediately(self) -> None:
        access = self.repo.create_business(owner_user_id=101, name="Практика")
        owner = self.repo.resolve_context(user_id=101, business_id=access.business.id)
        self.repo.grant_member(actor=owner, user_id=202, role=PlatformRole.SUPPORT)
        support = self.repo.resolve_context(user_id=202, business_id=access.business.id)
        self.assertEqual(support.role, PlatformRole.SUPPORT)

        revoked = self.repo.revoke_member(actor=owner, user_id=202)
        self.assertEqual(revoked.status, MembershipStatus.REVOKED)
        with self.assertRaises(TenantAccessDenied):
            self.repo.resolve_context(user_id=202, business_id=access.business.id)

    def test_administrator_cannot_escalate_or_modify_owner(self) -> None:
        access = self.repo.create_business(owner_user_id=101, name="Практика")
        owner = self.repo.resolve_context(user_id=101, business_id=access.business.id)
        self.repo.grant_member(actor=owner, user_id=202, role=PlatformRole.ADMINISTRATOR)
        administrator = self.repo.resolve_context(user_id=202, business_id=access.business.id)

        analyst = self.repo.grant_member(
            actor=administrator,
            user_id=303,
            role=PlatformRole.ANALYST,
        )
        self.assertEqual(analyst.role, PlatformRole.ANALYST)
        with self.assertRaises(TenantPermissionDenied):
            self.repo.grant_member(actor=administrator, user_id=404, role=PlatformRole.OWNER)
        with self.assertRaises(TenantPermissionDenied):
            self.repo.revoke_member(actor=administrator, user_id=101)

    def test_customer_cannot_be_stored_as_business_member(self) -> None:
        access = self.repo.create_business(owner_user_id=101, name="Практика")
        owner = self.repo.resolve_context(user_id=101, business_id=access.business.id)
        with self.assertRaises(ValueError):
            self.repo.grant_member(actor=owner, user_id=202, role=PlatformRole.CUSTOMER)

    def test_last_active_owner_cannot_be_revoked(self) -> None:
        access = self.repo.create_business(owner_user_id=101, name="Практика")
        owner = self.repo.resolve_context(user_id=101, business_id=access.business.id)
        with self.assertRaises(TenantInvariantViolation):
            self.repo.revoke_member(actor=owner, user_id=101)
        still_owner = self.repo.resolve_context(user_id=101, business_id=access.business.id)
        self.assertEqual(still_owner.role, PlatformRole.OWNER)

    def test_last_active_owner_cannot_be_demoted_through_role_update(self) -> None:
        access = self.repo.create_business(owner_user_id=101, name="Практика")
        owner = self.repo.resolve_context(user_id=101, business_id=access.business.id)
        with self.assertRaises(TenantInvariantViolation):
            self.repo.grant_member(actor=owner, user_id=101, role=PlatformRole.MANAGER)
        still_owner = self.repo.resolve_context(user_id=101, business_id=access.business.id)
        self.assertEqual(still_owner.role, PlatformRole.OWNER)

    def test_owner_can_be_demoted_after_second_owner_is_active(self) -> None:
        access = self.repo.create_business(owner_user_id=101, name="Практика")
        owner = self.repo.resolve_context(user_id=101, business_id=access.business.id)
        self.repo.grant_member(actor=owner, user_id=202, role=PlatformRole.OWNER)
        demoted = self.repo.grant_member(actor=owner, user_id=101, role=PlatformRole.MANAGER)
        self.assertEqual(demoted.role, PlatformRole.MANAGER)
        remaining_owner = self.repo.resolve_context(user_id=202, business_id=access.business.id)
        self.assertEqual(remaining_owner.role, PlatformRole.OWNER)


    def test_owner_onboarding_session_is_durable_channel_scoped_and_tenant_safe(self) -> None:
        first = self.repo.create_business(owner_user_id=101, name="Практика")
        outsider = self.repo.create_business(owner_user_id=202, name="Чужой бизнес")

        started = self.repo.set_owner_onboarding_session(
            user_id=101,
            platform="vk",
            step=OwnerOnboardingStep.BUSINESS_NAME,
        )
        self.assertEqual(started.step, OwnerOnboardingStep.BUSINESS_NAME)
        self.assertIsNone(started.business_id)
        self.assertIsNone(
            self.repo.get_owner_onboarding_session(user_id=101, platform="max")
        )

        continued = self.repo.set_owner_onboarding_session(
            user_id=101,
            platform="vk",
            step=OwnerOnboardingStep.ACTIVITY_DESCRIPTION,
            business_id=first.business.id,
        )
        restored = TenancyRepository(self.conn).get_owner_onboarding_session(
            user_id=101, platform="vk"
        )
        self.assertEqual(restored, continued)

        with self.assertRaises(TenantAccessDenied):
            self.repo.set_owner_onboarding_session(
                user_id=101,
                platform="max",
                step=OwnerOnboardingStep.ACTIVITY_DESCRIPTION,
                business_id=outsider.business.id,
            )

        self.repo.clear_owner_onboarding_session(user_id=101, platform="vk")
        self.assertIsNone(
            self.repo.get_owner_onboarding_session(user_id=101, platform="vk")
        )

    def test_owner_control_workspace_is_durable_channel_scoped_and_authorized(self) -> None:
        first = self.repo.create_business(owner_user_id=101, name="Практика")
        second = self.repo.create_business(owner_user_id=101, name="Школа")
        outsider = self.repo.create_business(owner_user_id=202, name="Чужой бизнес")

        self.repo.set_owner_control_workspace(
            user_id=101, platform="vk", business_id=first.business.id
        )
        self.repo.set_owner_control_workspace(
            user_id=101, platform="max", business_id=second.business.id
        )

        self.assertEqual(
            self.repo.get_owner_control_workspace(user_id=101, platform="vk"),
            first.business.id,
        )
        self.assertEqual(
            self.repo.get_owner_control_workspace(user_id=101, platform="max"),
            second.business.id,
        )
        with self.assertRaises(TenantAccessDenied):
            self.repo.set_owner_control_workspace(
                user_id=101, platform="vk", business_id=outsider.business.id
            )

    def test_owner_control_workspace_fails_closed_after_membership_revocation(self) -> None:
        access = self.repo.create_business(owner_user_id=101, name="Практика")
        owner = self.repo.resolve_context(user_id=101, business_id=access.business.id)
        self.repo.grant_member(actor=owner, user_id=202, role=PlatformRole.MANAGER)
        self.repo.set_owner_control_workspace(
            user_id=202, platform="max", business_id=access.business.id
        )
        self.assertEqual(
            self.repo.get_owner_control_workspace(user_id=202, platform="max"),
            access.business.id,
        )

        self.repo.revoke_member(actor=owner, user_id=202)
        self.assertIsNone(
            self.repo.get_owner_control_workspace(user_id=202, platform="max")
        )


if __name__ == "__main__":
    unittest.main()
