from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from clientplatform.application import cockpit, cockpit_growth_analytics, cockpit_money, cockpit_services
from clientplatform.application.cockpit_action_routing import (
    CockpitActionStartRoute,
    build_cockpit_action_start_payload,
    parse_cockpit_action_start_payload,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied
from handlers import clientplatform_cockpit_dispatch as cockpit_dispatch

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "22222222-2222-4222-8222-222222222222"
_REQUEST = "88888888-8888-4888-8888-888888888888"
_CAPABILITY = "33333333-3333-4333-8333-333333333333"


def _actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=role,
    )


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        onboarding_required=False,
        business_id=_BUSINESS,
        business_name="Практика",
        user_id=101,
    )


def _summary() -> SimpleNamespace:
    return SimpleNamespace(paid_payments=0, paid_customers=0, by_currency=())


def _advertising() -> SimpleNamespace:
    return SimpleNamespace(
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 7),
        connected_accounts=1,
        tracked_ads=2,
        impressions=1000,
        clicks=100,
        leads=8,
        bookings=4,
        won=2,
        ctr_percent=10.0,
    )


class CockpitPr308ReviewFindingTests(unittest.TestCase):
    def test_money_customer_selector_is_bounded_to_fifty(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        customer = SimpleNamespace(
            id="77777777-7777-4777-8777-777777777777",
            display_name="Анна",
        )
        with (
            patch.object(cockpit_money, "resolve_tenant_context", return_value=actor),
            patch.object(cockpit_money.admin_ops, "payment_summary", return_value=_summary()),
            patch.object(cockpit_money.admin_ops, "list_payments", return_value=[]),
            patch.object(cockpit_money, "search_customers", return_value=([customer], False)) as search,
            patch.object(cockpit_money, "list_business_capabilities", return_value=[]),
        ):
            snapshot = cockpit_money.build_cockpit_money(
                actor=actor,
                business_name="Практика",
            )
        search.assert_called_once_with(actor=actor, limit=50, offset=0)
        self.assertEqual(snapshot.customer_choices[0].id, customer.id)

    def test_service_creation_delegates_stable_uuid_to_canonical_idempotency_owner(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        capability = SimpleNamespace(id=_CAPABILITY)
        result = SimpleNamespace(id="44444444-4444-4444-8444-444444444444")
        with (
            patch.object(cockpit_services, "_resolve_actor", return_value=(actor, "Практика")),
            patch.object(cockpit_services, "_offering_capabilities", return_value=[capability]),
            patch.object(cockpit_services, "create_business_offering", return_value=result) as create,
        ):
            actual = cockpit_services.create_cockpit_service(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                capability_id=_CAPABILITY,
                title="Консультация",
                description="60 минут",
                request_id=_REQUEST,
            )
        self.assertIs(actual, result)
        create.assert_called_once_with(
            actor=actor,
            capability_id=_CAPABILITY,
            title="Консультация",
            description="60 минут",
            idempotency_key=f"cockpit-service:{_REQUEST}",
        )

    def test_growth_roles_without_business_ledger_receive_advertising_only_projection(self) -> None:
        for role in (PlatformRole.MARKETER, PlatformRole.CONTENT_MANAGER, PlatformRole.ANALYST):
            with self.subTest(role=role.value):
                actor = _actor(role)
                with (
                    patch.object(cockpit_growth_analytics, "resolve_tenant_context", return_value=actor),
                    patch.object(cockpit_growth_analytics, "get_growth_cockpit") as full_growth,
                    patch.object(
                        cockpit_growth_analytics,
                        "get_yandex_growth_snapshot",
                        return_value=_advertising(),
                    ),
                ):
                    snapshot = cockpit_growth_analytics.build_cockpit_growth_analytics(
                        actor=actor,
                        business_name="Практика",
                        period_days=7,
                    )
                full_growth.assert_not_called()
                self.assertFalse(snapshot.business_results_available)
                self.assertEqual(snapshot.metrics, ())
                self.assertEqual(snapshot.revenue, ())
                self.assertEqual(snapshot.sources, ())
                self.assertEqual(snapshot.actions, ())
                self.assertIsNotNone(snapshot.advertising)
                self.assertEqual(snapshot.advertising.won, 2)
                self.assertIsNone(snapshot.journey.paid_customers)

    def test_existing_action_payloads_remain_actions_not_sections(self) -> None:
        expected = {
            "economic_reactivation": "r",
            "economic_paid_acquisition": "d",
        }
        for action_key, kind in expected.items():
            with self.subTest(action_key=action_key):
                payload = build_cockpit_action_start_payload(
                    business_id=_BUSINESS,
                    action_key=action_key,
                )
                parsed = parse_cockpit_action_start_payload(payload)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.business_id, _BUSINESS)
                self.assertEqual(parsed.kind, kind)
                self.assertIsNone(parsed.section)

    def test_hidden_reactivation_route_preserves_support_roles_but_denies_customer(self) -> None:
        for role in (
            PlatformRole.OWNER,
            PlatformRole.ADMINISTRATOR,
            PlatformRole.MANAGER,
            PlatformRole.SUPPORT,
        ):
            with self.subTest(role=role.value):
                actor = _actor(role)
                with (
                    patch.object(cockpit, "resolve_cockpit_context", return_value=_context()),
                    patch.object(cockpit, "resolve_tenant_context", return_value=actor),
                ):
                    payload = cockpit.resolve_cockpit_section_start_payload(
                        telegram_user_id=101,
                        requested_business_id=_BUSINESS,
                        section="reactivation",
                    )
                parsed = parse_cockpit_action_start_payload(payload)
                self.assertEqual(parsed.kind, "q")
                self.assertEqual(parsed.section, "reactivation")

        with (
            patch.object(cockpit, "resolve_cockpit_context", return_value=_context()),
            patch.object(cockpit, "resolve_tenant_context", return_value=_actor(PlatformRole.CUSTOMER)),
        ):
            with self.assertRaises(TenantPermissionDenied):
                cockpit.resolve_cockpit_section_start_payload(
                    telegram_user_id=101,
                    requested_business_id=_BUSINESS,
                    section="reactivation",
                )

    def test_hidden_ad_spend_route_remains_owner_only(self) -> None:
        with (
            patch.object(cockpit, "resolve_cockpit_context", return_value=_context()),
            patch.object(cockpit, "resolve_tenant_context", return_value=_actor(PlatformRole.OWNER)),
        ):
            payload = cockpit.resolve_cockpit_section_start_payload(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                section="ad-spend",
            )
        parsed = parse_cockpit_action_start_payload(payload)
        self.assertEqual(parsed.kind, "p")
        self.assertEqual(parsed.section, "ad-spend")

        for role in (PlatformRole.ADMINISTRATOR, PlatformRole.MANAGER, PlatformRole.MARKETER):
            with self.subTest(role=role.value):
                with (
                    patch.object(cockpit, "resolve_cockpit_context", return_value=_context()),
                    patch.object(cockpit, "resolve_tenant_context", return_value=_actor(role)),
                ):
                    with self.assertRaises(TenantPermissionDenied):
                        cockpit.resolve_cockpit_section_start_payload(
                            telegram_user_id=101,
                            requested_business_id=_BUSINESS,
                            section="ad-spend",
                        )

    def test_workspace_keeps_operation_uuid_until_success_and_binds_mutations_to_snapshot(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "clientplatform" / "runtime" / "cockpit_business_workspace.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (!businessId || select.value !== businessId)", script)
        self.assertIn("if (!servicesRequestId) servicesRequestId = newRequestId();", script)
        self.assertIn("if (!moneyRequestId) moneyRequestId = newRequestId();", script)
        self.assertIn("request_id: servicesRequestId", script)
        self.assertIn("request_id: moneyRequestId", script)

        service_submit = script.index('servicesForm.addEventListener("submit"')
        service_catch = script.index("} catch (error) {", service_submit)
        service_success_reset = script.index("resetServiceRequest();", service_submit)
        self.assertLess(service_success_reset, service_catch)
        self.assertNotIn("resetServiceRequest();", script[service_catch:script.index("});", service_catch) + 3])

        money_submit = script.index('moneyForm.addEventListener("submit"')
        money_catch = script.index("} catch (error) {", money_submit)
        money_success_reset = script.index("resetMoneyRequest();", money_submit)
        self.assertLess(money_success_reset, money_catch)
        self.assertNotIn("resetMoneyRequest();", script[money_catch:script.index("});", money_catch) + 3])


class CockpitPr308CanonicalDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_and_existing_action_routes_dispatch_to_exact_native_interactions(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        cases = (
            (CockpitActionStartRoute(business_id=_BUSINESS, kind="q", section="reactivation"), "cpm:reactivate"),
            (CockpitActionStartRoute(business_id=_BUSINESS, kind="r", section=None), "cpm:reactivate"),
            (CockpitActionStartRoute(business_id=_BUSINESS, kind="p", section="ad-spend"), "cpm:ad-spend"),
            (CockpitActionStartRoute(business_id=_BUSINESS, kind="d", section=None), "cpm:ad-spend"),
        )
        for route, raw_text in cases:
            with self.subTest(kind=route.kind, section=route.section):
                target = SimpleNamespace(answer=AsyncMock())
                rendered = SimpleNamespace(
                    text="canonical",
                    rows=((SimpleNamespace(label="Открыть", command="cpm:test"),),),
                )
                renderer = Mock(return_value=rendered)
                with (
                    patch.object(cockpit_dispatch, "resolve_tenant_context", return_value=actor),
                    patch.object(cockpit_dispatch, "render_native_member_interaction", new=renderer),
                    patch.object(cockpit_dispatch.one_click.control, "_keyboard", return_value="keyboard"),
                ):
                    await cockpit_dispatch.send_cockpit_action_route(
                        target,
                        user_id=101,
                        route=route,
                    )
                self.assertEqual(renderer.call_args.kwargs["raw_text"], raw_text)
                self.assertEqual(renderer.call_args.kwargs["actor"], actor)
                target.answer.assert_awaited_once_with("canonical", reply_markup="keyboard")


if __name__ == "__main__":
    unittest.main()
