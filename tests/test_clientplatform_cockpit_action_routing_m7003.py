from __future__ import annotations

import unittest
from unittest.mock import patch

from clientplatform.application import cockpit_customers
from clientplatform.application.cockpit_action_routing import (
    build_cockpit_action_start_payload,
    build_cockpit_section_start_payload,
    parse_cockpit_action_start_payload,
)
from clientplatform.application.growth_cockpit import GrowthAction
from clientplatform.domain.customers import Customer, CustomerRecord, CustomerStatus
from clientplatform.domain.tenancy import PlatformRole, TenantAccessDenied, TenantContext

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "22222222-2222-4222-8222-222222222222"
_CUSTOMER = "33333333-3333-4333-8333-333333333333"
_LEAD = "44444444-4444-4444-8444-444444444444"
_PLAN = "55555555-5555-4555-8555-555555555555"


def _actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS, user_id=101, membership_id=_MEMBER, role=role
    )


def _record() -> CustomerRecord:
    return CustomerRecord(
        customer=Customer(
            id=_CUSTOMER,
            business_id=_BUSINESS,
            display_name="Анна",
            status=CustomerStatus.ACTIVE,
            created_by_member_id=_MEMBER,
            created_at="2026-09-01T10:00:00+00:00",
            updated_at="2026-09-05T10:00:00+00:00",
        ),
        identities=(),
    )


class CockpitActionRoutingM7003Tests(unittest.TestCase):
    def test_start_payload_round_trips_only_supported_existing_sales_routes(self) -> None:
        cases = (
            ("sales_handoff", "h", None),
            (f"sales_plan:{_PLAN}", "w", None),
            (f"sales_lead:{_LEAD}", "l", _LEAD),
        )
        for action_key, kind, lead_id in cases:
            with self.subTest(action_key=action_key):
                payload = build_cockpit_action_start_payload(
                    business_id=_BUSINESS, action_key=action_key
                )
                self.assertLessEqual(len(payload), 64)
                self.assertRegex(payload, r"^[A-Za-z0-9_-]+$")
                parsed = parse_cockpit_action_start_payload(payload)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(parsed.business_id, _BUSINESS)
                self.assertEqual(parsed.kind, kind)
                self.assertEqual(parsed.lead_id, lead_id)

    def test_section_start_payload_round_trips_supported_cockpit_sections(self) -> None:
        sections = (
            "calendar",
            "sales",
            "growth",
            "content",
            "automation",
            "analytics",
            "connections",
            "team",
            "settings",
        )
        for section in sections:
            with self.subTest(section=section):
                payload = build_cockpit_section_start_payload(
                    business_id=_BUSINESS, section=section
                )
                self.assertLessEqual(len(payload), 64)
                parsed = parse_cockpit_action_start_payload(payload)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(parsed.business_id, _BUSINESS)
                self.assertEqual(parsed.section, section)
                self.assertIsNone(parsed.lead_id)
        with self.assertRaises(ValueError):
            build_cockpit_section_start_payload(
                business_id=_BUSINESS, section="billing"
            )

    def test_route_payload_fails_closed_for_unknown_or_malformed_action(self) -> None:
        with self.assertRaises(ValueError):
            build_cockpit_action_start_payload(
                business_id=_BUSINESS, action_key="future_customer_action"
            )
        with self.assertRaises(ValueError):
            parse_cockpit_action_start_payload("cpo_c_not-a-valid-route")
        self.assertIsNone(parse_cockpit_action_start_payload("cpo_landing"))

    def test_route_reloads_customer_and_current_canonical_action(self) -> None:
        calls: list[tuple[str, str, int]] = []
        action = GrowthAction(
            title="Открыть клиента",
            reason="Есть сохранённый следующий шаг.",
            action_key=f"sales_lead:{_LEAD}",
            source="sales_lead",
            source_id=_LEAD,
        )

        def record_loader(*, actor: TenantContext, customer_id: str):
            self.assertEqual(actor.business_id, _BUSINESS)
            self.assertEqual(customer_id, _CUSTOMER)
            return _record()

        def action_loader(*, actor: TenantContext, customer_id: str, limit: int):
            calls.append((actor.business_id, customer_id, limit))
            return (action,)

        route = cockpit_customers.build_cockpit_customer_action_route(
            actor=_actor(),
            customer_id=_CUSTOMER,
            record_loader=record_loader,
            action_loader=action_loader,
        )
        self.assertEqual(calls, [(_BUSINESS, _CUSTOMER, 1)])
        self.assertEqual(route.action_key, action.action_key)
        parsed = parse_cockpit_action_start_payload(route.start_payload)
        assert parsed is not None
        self.assertEqual(parsed.lead_id, _LEAD)

    def test_route_fails_closed_when_visible_action_became_stale(self) -> None:
        current = GrowthAction(
            title="Открыть клиента",
            reason="Новый шаг.",
            action_key=f"sales_lead:{_LEAD}",
            source="sales_lead",
            source_id=_LEAD,
        )
        with self.assertRaises(cockpit_customers.CockpitCustomerActionUnavailable):
            cockpit_customers.build_cockpit_customer_action_route(
                actor=_actor(),
                customer_id=_CUSTOMER,
                expected_action_key="sales_handoff",
                record_loader=lambda **_: _record(),
                action_loader=lambda **_: (current,),
            )

    def test_route_fails_when_current_action_disappears(self) -> None:
        with self.assertRaises(cockpit_customers.CockpitCustomerActionUnavailable):
            cockpit_customers.build_cockpit_customer_action_route(
                actor=_actor(),
                customer_id=_CUSTOMER,
                record_loader=lambda **_: _record(),
                action_loader=lambda **_: (),
            )

    def test_revoked_membership_stops_route_before_customer_read(self) -> None:
        context = type(
            "Context",
            (),
            {"onboarding_required": False, "business_id": _BUSINESS, "user_id": 101},
        )()
        with (
            patch.object(cockpit_customers, "resolve_cockpit_context", return_value=context),
            patch.object(
                cockpit_customers,
                "resolve_tenant_context",
                side_effect=TenantAccessDenied("membership revoked"),
            ),
            patch.object(cockpit_customers, "build_cockpit_customer_action_route") as build,
        ):
            with self.assertRaises(TenantAccessDenied):
                cockpit_customers.resolve_cockpit_customer_action_route(
                    telegram_user_id=101,
                    requested_business_id=_BUSINESS,
                    customer_id=_CUSTOMER,
                )
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
