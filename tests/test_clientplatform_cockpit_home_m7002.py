from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import cockpit_home, growth_cockpit
from clientplatform.application.cockpit import CockpitContext
from clientplatform.application.growth_cockpit import GrowthAction
from clientplatform.domain.tenancy import (
    PlatformRole,
    TenantAccessDenied,
    TenantContext,
    TenantPermissionDenied,
)

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_NOW = datetime(2026, 9, 3, 21, 30, tzinfo=timezone.utc)


def _actor(role: PlatformRole) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=role,
    )


def _profile() -> SimpleNamespace:
    return SimpleNamespace(timezone="Europe/Tallinn")


def _growth(actor: TenantContext, **_kwargs: object) -> SimpleNamespace:
    if actor.role not in {PlatformRole.OWNER, PlatformRole.ADMINISTRATOR, PlatformRole.MANAGER}:
        raise TenantPermissionDenied("money denied")
    return SimpleNamespace(
        today_metrics=(
            SimpleNamespace(key="leads", value=3, source="ledger", meaning="Лиды за локальный день."),
            SimpleNamespace(key="bookings", value=2, source="ledger", meaning="Записи за локальный день."),
            SimpleNamespace(key="paid_customers", value=1, source="ledger", meaning="Оплаты за локальный день."),
        ),
        revenue=(
            SimpleNamespace(amount_minor=12345, currency="RUB", source="revenue", meaning="Подтверждено."),
            SimpleNamespace(amount_minor=2500, currency="USD", source="revenue", meaning="Подтверждено."),
        ),
        attention=("Есть клиент, которому требуется ответ.",),
        actions=(
            GrowthAction(
                title="Ответить лично: Анна",
                reason="Срочная передача: клиенту требуется личное участие сотрудника.",
                action_key="sales_handoff",
                source="sales_handoff_queue",
            ),
        ),
        limitations=(),
    )


def _customer_activity(actor: TenantContext, **_kwargs: object) -> SimpleNamespace:
    if actor.role not in {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.SUPPORT,
    }:
        raise TenantPermissionDenied("customers denied")
    return SimpleNamespace(total=12)


def _bookings(actor: TenantContext, **_kwargs: object) -> list[SimpleNamespace]:
    if actor.role not in {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.SUPPORT,
    }:
        raise TenantPermissionDenied("bookings denied")
    return [
        SimpleNamespace(slot=SimpleNamespace(starts_at="2026-09-03T21:45:00+00:00", status="open")),
        SimpleNamespace(slot=SimpleNamespace(starts_at="2026-09-03T22:30:00+00:00", status="booked")),
        SimpleNamespace(slot=SimpleNamespace(starts_at="2026-09-03T20:30:00+00:00", status="open")),
    ]


def _customer_work(actor: TenantContext, **_kwargs: object) -> tuple[GrowthAction, ...]:
    if actor.role not in {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.SUPPORT,
    }:
        raise TenantPermissionDenied("sales denied")
    return (
        GrowthAction(
            title="Следующий шаг по клиенту: Борис",
            reason="Сохранён следующий шаг: перезвонить.",
            action_key="sales_lead:opaque",
            source="sales_lead",
        ),
    )


def _approvals(actor: TenantContext, **_kwargs: object) -> tuple[object, ...]:
    if actor.role not in {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.MARKETER,
    }:
        raise TenantPermissionDenied("automation denied")
    return (object(), object())


class CockpitHomeM7002Tests(unittest.TestCase):
    def _build(self, role: PlatformRole, **overrides: object) -> cockpit_home.CockpitHomeSnapshot:
        actor = _actor(role)
        kwargs = {
            "growth_loader": _growth,
            "customer_work_loader": _customer_work,
            "customer_activity_loader": _customer_activity,
            "booking_loader": _bookings,
            "approval_loader": _approvals,
        }
        kwargs.update(overrides)
        with (
            patch.object(cockpit_home, "resolve_tenant_context", return_value=actor),
            patch.object(cockpit_home, "get_business_profile", return_value=_profile()),
        ):
            return cockpit_home.build_cockpit_home(
                actor=actor,
                business_name="Практика",
                now=_NOW,
                **kwargs,
            )

    def test_role_visibility_never_expands_under_home_projection(self) -> None:
        rich = {PlatformRole.OWNER, PlatformRole.ADMINISTRATOR, PlatformRole.MANAGER}
        for role in PlatformRole:
            if role == PlatformRole.CUSTOMER:
                continue
            with self.subTest(role=role.value):
                result = self._build(role)
                keys = {item.key for item in result.metrics}
                if role in rich:
                    self.assertIn("today_leads", keys)
                    self.assertIn("customers_total", keys)
                    self.assertEqual({item.currency for item in result.money}, {"RUB", "USD"})
                elif role == PlatformRole.SUPPORT:
                    self.assertNotIn("today_leads", keys)
                    self.assertIn("customers_total", keys)
                    self.assertEqual(result.money, ())
                    self.assertTrue(any(item.section == "sales" for item in result.actions))
                elif role == PlatformRole.MARKETER:
                    self.assertEqual(keys, {"automation_pending"})
                    self.assertEqual(result.money, ())
                    self.assertFalse(any(item.section == "automation" for item in result.actions))
                    self.assertEqual(result.actions[0].section, "growth")
                else:
                    self.assertEqual(result.money, ())
                    self.assertNotIn("customers_total", keys)
                    self.assertIsNotNone(result.empty_message)

    def test_business_local_day_boundary_counts_only_tallinn_today(self) -> None:
        result = self._build(PlatformRole.SUPPORT)
        metrics = {item.key: item.value for item in result.metrics}
        self.assertEqual(result.timezone_name, "Europe/Tallinn")
        self.assertEqual(result.today_from, "2026-09-03T21:00:00+00:00")
        self.assertEqual(result.today_to, "2026-09-04T21:00:00+00:00")
        self.assertEqual(metrics["open_slots_today"], 1)
        self.assertEqual(metrics["booked_slots_today"], 1)

    def test_money_is_currency_safe_and_never_combined(self) -> None:
        result = self._build(PlatformRole.OWNER)
        self.assertEqual(
            [(item.amount_minor, item.currency, item.display) for item in result.money],
            [(12345, "RUB", "123.45 RUB"), (2500, "USD", "25.00 USD")],
        )
        self.assertFalse(any(item.key == "revenue_total" for item in result.metrics))

    def test_partial_source_failure_is_explicit_not_zero_filled(self) -> None:
        def broken_growth(**_kwargs: object) -> object:
            raise OSError("temporary")

        result = self._build(PlatformRole.OWNER, growth_loader=broken_growth)
        keys = {item.key for item in result.metrics}
        self.assertIn("economics_unavailable", result.limitations)
        self.assertNotIn("today_leads", keys)
        self.assertEqual(result.money, ())
        self.assertIn("customers_total", keys)


    def test_malformed_optional_money_and_booking_sources_are_not_zero_filled(self) -> None:
        def bad_currency(actor: TenantContext, **_kwargs: object) -> SimpleNamespace:
            result = _growth(actor)
            return SimpleNamespace(
                today_metrics=result.today_metrics,
                revenue=(SimpleNamespace(amount_minor=1, currency="XXX", source="revenue", meaning="bad"),),
                attention=(),
                actions=(),
                limitations=(),
            )

        def bad_booking(actor: TenantContext, **_kwargs: object) -> list[SimpleNamespace]:
            if actor.role != PlatformRole.OWNER:
                raise TenantPermissionDenied("denied")
            return [SimpleNamespace(slot=SimpleNamespace(starts_at="not-a-date", status="open"))]

        result = self._build(
            PlatformRole.OWNER,
            growth_loader=bad_currency,
            booking_loader=bad_booking,
        )
        keys = {item.key for item in result.metrics}
        self.assertEqual(result.money, ())
        self.assertIn("economics_currency_unavailable", result.limitations)
        self.assertIn("booking_unavailable", result.limitations)
        self.assertNotIn("open_slots_today", keys)
        self.assertNotIn("booked_slots_today", keys)

    def test_revoked_membership_fails_closed_even_during_optional_source(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        with (
            patch.object(
                cockpit_home,
                "resolve_tenant_context",
                side_effect=TenantAccessDenied("revoked"),
            ),
            self.assertRaises(TenantAccessDenied),
        ):
            cockpit_home.build_cockpit_home(actor=actor, business_name="Практика", now=_NOW)

        def revoked_growth(**_kwargs: object) -> object:
            raise TenantAccessDenied("revoked during read")

        with (
            patch.object(cockpit_home, "resolve_tenant_context", return_value=actor),
            patch.object(cockpit_home, "get_business_profile", return_value=_profile()),
            self.assertRaises(TenantAccessDenied),
        ):
            cockpit_home.build_cockpit_home(
                actor=actor,
                business_name="Практика",
                now=_NOW,
                growth_loader=revoked_growth,
            )

    def test_refresh_is_deterministic_and_read_only_with_same_sources(self) -> None:
        first = self._build(PlatformRole.OWNER)
        second = self._build(PlatformRole.OWNER)
        self.assertEqual(first, second)
        self.assertEqual(first.schema_version, "2026-09-04.v1")
        self.assertLessEqual(len(first.metrics), 10)
        self.assertLessEqual(len(first.actions), 5)
        self.assertLessEqual(len(first.attention), 8)

    def test_resolve_home_rechecks_server_scope_and_rejects_cross_tenant(self) -> None:
        context = CockpitContext(
            user_id=101,
            business_id=_BUSINESS,
            business_name="Практика",
            role="owner",
            onboarding_required=False,
            businesses=(),
            navigation=(),
        )
        actor = _actor(PlatformRole.OWNER)
        with (
            patch.object(cockpit_home, "resolve_cockpit_context", return_value=context),
            patch.object(cockpit_home, "resolve_tenant_context", return_value=actor) as live,
            patch.object(cockpit_home, "build_cockpit_home", return_value=self._build(PlatformRole.OWNER)),
        ):
            cockpit_home.resolve_cockpit_home(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                now=_NOW,
            )
        live.assert_called_with(user_id=101, business_id=_BUSINESS)

        with (
            patch.object(
                cockpit_home,
                "resolve_cockpit_context",
                side_effect=TenantAccessDenied("foreign business"),
            ),
            self.assertRaises(TenantAccessDenied),
        ):
            cockpit_home.resolve_cockpit_home(
                telegram_user_id=101,
                requested_business_id="22222222-2222-4222-8222-222222222222",
            )

    def test_customer_work_actions_preserve_handoff_then_sales_order(self) -> None:
        actor = _actor(PlatformRole.SUPPORT)
        handoffs = [
            {"id": "h1", "lead_id": "lead-1", "customer_name": "Анна", "severity": "urgent"},
        ]
        sales = [
            {"id": "lead-1", "customer_name": "Анна", "next_action": "duplicate"},
            {"id": "lead-2", "customer_name": "Борис", "next_action": "Позвонить"},
        ]
        with (
            patch.object(growth_cockpit, "count_sales_handoff_work", return_value=1),
            patch.object(growth_cockpit, "list_sales_handoff_work", return_value=handoffs),
            patch.object(growth_cockpit, "list_sales_work", return_value=sales),
        ):
            actions = growth_cockpit.get_customer_work_actions(actor=actor, limit=5)
        self.assertEqual([item.title for item in actions], ["Ответить лично: Анна", "Следующий шаг по клиенту: Борис"])


if __name__ == "__main__":
    unittest.main()
