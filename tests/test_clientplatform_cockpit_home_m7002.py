from __future__ import annotations

import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import cockpit, cockpit_home
from clientplatform.application.growth_cockpit import GrowthAction
from clientplatform.domain.tenancy import (
    PlatformRole,
    TenantAccessDenied,
    TenantContext,
    TenantPermissionDenied,
)

_BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
_MEMBERSHIP_ID = "22222222-2222-4222-8222-222222222222"
_NOW = datetime(2026, 9, 3, 22, 30, tzinfo=timezone.utc)


def _actor(role: PlatformRole) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS_ID,
        user_id=1001,
        membership_id=_MEMBERSHIP_ID,
        role=role,
    )

def _growth_snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        today_metrics=(
            SimpleNamespace(key="leads", value=3, meaning="Подтверждённые лиды за локальный день."),
            SimpleNamespace(key="bookings", value=2, meaning="Подтверждённые записи за локальный день."),
        ),
        revenue=(
            SimpleNamespace(amount_minor=12345, currency="RUB", meaning="Подтверждённая выручка."),
            SimpleNamespace(amount_minor=250, currency="USD", meaning="Подтверждённая выручка."),
        ),
        attention=("Ответить горячему лиду.",),
        next_action=GrowthAction(
            title="Ответить клиенту",
            reason="Клиент ждёт личного ответа.",
            action_key="sales_handoff",
            source="sales_handoff_queue",
            source_id="handoff-1",
        ),
    )


def _approval_reader(*, actor: TenantContext, **_kwargs):
    if actor.role not in {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.MARKETER,
    }:
        raise TenantPermissionDenied("approval read denied")
    return (object(),) if actor.role == PlatformRole.MARKETER else ()

class CockpitHomeM7002Tests(unittest.TestCase):
    def _base_patches(self, *, growth=None, activity=None, approvals=None) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(
            patch.object(cockpit_home, "get_business_profile", return_value=SimpleNamespace(timezone="Europe/Tallinn"))
        )
        stack.enter_context(
            patch.object(
                cockpit_home,
                "get_growth_cockpit",
                side_effect=growth if callable(growth) else None,
                return_value=_growth_snapshot() if growth is None else None,
            )
        )
        stack.enter_context(
            patch.object(
                cockpit_home,
                "tenant_customer_activity",
                return_value=activity or SimpleNamespace(new_today=2, active_today=4),
            )
        )
        stack.enter_context(patch.object(cockpit_home, "list_booking_slots", return_value=[]))
        stack.enter_context(patch.object(cockpit_home, "list_sales_handoff_work", return_value=[]))
        stack.enter_context(patch.object(cockpit_home, "list_sales_work", return_value=[]))
        stack.enter_context(
            patch.object(cockpit_home, "list_pending_automation_action_approvals", side_effect=approvals or _approval_reader)
        )
        return stack

    def test_owner_projection_uses_business_day_currency_rows_and_inert_ad_loader(self) -> None:
        seen_advertising: list[object] = []

        def growth_loader(**kwargs):
            seen_advertising.append(kwargs["advertising_loader"](actor=kwargs["actor"]))
            return _growth_snapshot()

        with self._base_patches(growth=growth_loader):
            projection = cockpit_home.get_cockpit_home(actor=_actor(PlatformRole.OWNER), now=_NOW)

        self.assertEqual(projection.schema_version, 1)
        self.assertEqual(projection.timezone_name, "Europe/Tallinn")
        self.assertEqual(projection.today_from, "2026-09-03T21:00:00+00:00")
        self.assertEqual(projection.today_to, "2026-09-04T21:00:00+00:00")
        self.assertEqual(seen_advertising, [None])
        self.assertEqual([(item.currency, item.amount_minor) for item in projection.money], [("RUB", 12345), ("USD", 250)])
        self.assertEqual([item.display_amount for item in projection.money], ["123.45 RUB", "2.50 USD"])
        self.assertEqual(projection.next_action.route, "sales")
        keys = {item.key for item in projection.today}
        self.assertTrue({"leads", "bookings", "new_customers_today", "active_customers_today"}.issubset(keys))

    def test_role_visibility_never_leaks_customer_or_money_facts(self) -> None:
        customer_roles = {
            PlatformRole.OWNER,
            PlatformRole.ADMINISTRATOR,
            PlatformRole.MANAGER,
            PlatformRole.SUPPORT,
        }
        money_roles = {PlatformRole.OWNER, PlatformRole.ADMINISTRATOR, PlatformRole.MANAGER}
        roles = (
            PlatformRole.OWNER,
            PlatformRole.ADMINISTRATOR,
            PlatformRole.MANAGER,
            PlatformRole.SUPPORT,
            PlatformRole.CONTENT_MANAGER,
            PlatformRole.MARKETER,
            PlatformRole.ANALYST,
        )
        with self._base_patches():
            projections = {role: cockpit_home.get_cockpit_home(actor=_actor(role), now=_NOW) for role in roles}

        for role, projection in projections.items():
            keys = {item.key for item in projection.today}
            self.assertEqual("new_customers_today" in keys, role in customer_roles)
            self.assertEqual(bool(projection.money), role in money_roles)
        self.assertTrue(any("Ждут решения" in item for item in projections[PlatformRole.MARKETER].attention))

    def test_partial_growth_failure_is_unavailable_not_false_zero(self) -> None:
        def broken_growth(**_kwargs):
            raise OSError("temporary read failure")

        with self._base_patches(growth=broken_growth):
            projection = cockpit_home.get_cockpit_home(actor=_actor(PlatformRole.OWNER), now=_NOW)

        self.assertEqual(projection.money, ())
        self.assertNotIn("leads", {item.key for item in projection.today})
        growth_source = next(item for item in projection.sources if item.id == "growth")
        self.assertEqual(growth_source.status, "unavailable")
        self.assertIn("недоступна", growth_source.message)

    def test_support_reuses_canonical_sales_handoff_order_for_next_step(self) -> None:
        with self._base_patches(), patch.object(
            cockpit_home,
            "list_sales_handoff_work",
            return_value=[{"id": "handoff-1", "lead_id": "lead-1", "severity": "urgent", "customer_name": "Анна"}],
        ):
            projection = cockpit_home.get_cockpit_home(actor=_actor(PlatformRole.SUPPORT), now=_NOW)

        self.assertIsNotNone(projection.next_action)
        self.assertEqual(projection.next_action.action_key, "sales_handoff")
        self.assertEqual(projection.next_action.route, "sales")
        self.assertFalse(projection.money)

    def test_home_actor_rechecks_live_membership_after_shell_context(self) -> None:
        context = cockpit.CockpitContext(
            user_id=1001,
            business_id=_BUSINESS_ID,
            business_name="Практика",
            role="owner",
            onboarding_required=False,
            businesses=(),
            navigation=(),
        )
        with (
            patch.object(cockpit, "resolve_cockpit_context", return_value=context),
            patch.object(cockpit, "resolve_tenant_context", side_effect=TenantAccessDenied("revoked")),
        ):
            with self.assertRaises(TenantAccessDenied):
                cockpit.resolve_cockpit_actor(telegram_user_id=1001, requested_business_id=_BUSINESS_ID)


if __name__ == "__main__":
    unittest.main()
