from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import (
    cockpit_growth_analytics,
    cockpit_money,
    cockpit_services,
)
from clientplatform.application.cockpit_action_routing import (
    build_cockpit_section_start_payload,
    parse_cockpit_action_start_payload,
)
from clientplatform.domain.activity import CapabilityKind, CapabilityStatus, OfferingStatus
from clientplatform.domain.attribution import AcquisitionSource
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "22222222-2222-4222-8222-222222222222"


def _actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=role,
    )


def _capability() -> SimpleNamespace:
    return SimpleNamespace(
        id="33333333-3333-4333-8333-333333333333",
        business_id=_BUSINESS,
        connector_key="services",
        kind=CapabilityKind.SERVICES,
        title="Услуги",
        status=CapabilityStatus.ACTIVE,
    )


def _offering() -> SimpleNamespace:
    return SimpleNamespace(
        id="44444444-4444-4444-8444-444444444444",
        business_id=_BUSINESS,
        capability_id=_capability().id,
        title="Консультация",
        description="60 минут",
        status=OfferingStatus.ACTIVE,
    )


class CockpitBusinessWorkspaceM7008Tests(unittest.TestCase):
    def test_marketer_services_can_price_but_cannot_archive_or_create(self) -> None:
        actor = _actor(PlatformRole.MARKETER)
        price = SimpleNamespace(
            offering_id=_offering().id,
            amount_minor=350_000,
            currency="RUB",
        )
        with (
            patch.object(cockpit_services, "resolve_tenant_context", return_value=actor),
            patch.object(cockpit_services, "list_business_capabilities", return_value=[_capability()]),
            patch.object(cockpit_services, "list_business_offerings", return_value=[_offering()]),
            patch.object(cockpit_services.admin_ops, "list_offering_prices", return_value=[price]),
        ):
            snapshot = cockpit_services.build_cockpit_services(actor=actor, business_name="Практика")
        self.assertFalse(snapshot.can_manage_offerings)
        self.assertTrue(snapshot.can_manage_prices)
        self.assertTrue(snapshot.can_view_prices)
        self.assertEqual(snapshot.items[0].price_display, "3 500.00 RUB")
        self.assertEqual(snapshot.items[0].price_input, "3500.00")

    def test_content_manager_services_never_leak_finance_price(self) -> None:
        actor = _actor(PlatformRole.CONTENT_MANAGER)
        with (
            patch.object(cockpit_services, "resolve_tenant_context", return_value=actor),
            patch.object(cockpit_services, "list_business_capabilities", return_value=[_capability()]),
            patch.object(cockpit_services, "list_business_offerings", return_value=[_offering()]),
            patch.object(cockpit_services.admin_ops, "list_offering_prices") as prices,
        ):
            snapshot = cockpit_services.build_cockpit_services(actor=actor, business_name="Практика")
        prices.assert_not_called()
        self.assertTrue(snapshot.can_manage_offerings)
        self.assertFalse(snapshot.can_view_prices)
        self.assertIsNone(snapshot.items[0].price_display)

    def test_marketer_money_read_does_not_require_or_expose_customer_records(self) -> None:
        actor = _actor(PlatformRole.MARKETER)
        summary = SimpleNamespace(
            paid_payments=2,
            paid_customers=1,
            by_currency=(SimpleNamespace(currency="RUB", amount_minor=700_000, paid_payments=2),),
        )
        payment = SimpleNamespace(
            id="55555555-5555-4555-8555-555555555555",
            amount_minor=350_000,
            currency="RUB",
            status="paid",
            note="Консультация",
            provider="manual",
            created_at="2026-09-07T09:00:00+00:00",
            outcome_event_id="event-1",
        )
        with (
            patch.object(cockpit_money, "resolve_tenant_context", return_value=actor),
            patch.object(cockpit_money.admin_ops, "payment_summary", return_value=summary),
            patch.object(cockpit_money.admin_ops, "list_payments", return_value=[payment]),
            patch.object(cockpit_money, "search_customers") as customers,
            patch.object(cockpit_money, "list_business_capabilities", return_value=[]),
        ):
            snapshot = cockpit_money.build_cockpit_money(actor=actor, business_name="Практика")
        customers.assert_not_called()
        self.assertTrue(snapshot.can_write)
        self.assertEqual(snapshot.customer_choices, ())
        self.assertEqual(snapshot.totals[0].display, "7 000.00 RUB")

    def test_marketer_cannot_smuggle_customer_id_through_money_endpoint_adapter(self) -> None:
        actor = _actor(PlatformRole.MARKETER)
        with (
            patch.object(cockpit_money, "_resolve_actor", return_value=(actor, "Практика")),
            patch.object(cockpit_money.admin_ops, "record_payment") as record,
        ):
            with self.assertRaises(TenantPermissionDenied):
                cockpit_money.record_cockpit_payment(
                    telegram_user_id=101,
                    requested_business_id=_BUSINESS,
                    amount="3500",
                    currency="RUB",
                    request_id="66666666-6666-4666-8666-666666666666",
                    customer_id="77777777-7777-4777-8777-777777777777",
                    offering_id=None,
                    note="",
                )
        record.assert_not_called()

    def test_payment_delegates_to_canonical_owner_with_idempotency_only(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        result = SimpleNamespace(id="payment-1")
        with (
            patch.object(cockpit_money, "_resolve_actor", return_value=(actor, "Практика")),
            patch.object(cockpit_money.admin_ops, "record_payment", return_value=result) as record,
        ):
            actual = cockpit_money.record_cockpit_payment(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                amount="3500.50",
                currency="rub",
                request_id="66666666-6666-4666-8666-666666666666",
                customer_id=None,
                offering_id=None,
                note="Оплата",
            )
        self.assertIs(actual, result)
        record.assert_called_once_with(
            actor=actor,
            amount_minor=350_050,
            currency="RUB",
            customer_id=None,
            offering_id=None,
            note="Оплата",
            idempotency_key="cockpit-payment:66666666-6666-4666-8666-666666666666",
        )

    def test_growth_projection_exposes_safe_results_not_provider_cost_or_credentials(self) -> None:
        actor = _actor(PlatformRole.OWNER)
        journey = SimpleNamespace(
            leads=8,
            bookings=4,
            completed_bookings=3,
            paid_customers=2,
            reactivated_customers=1,
            verified_revenue_by_currency=(SimpleNamespace(currency="RUB", amount_minor=900_000),),
            attributed_revenue_by_currency=(SimpleNamespace(currency="RUB", amount_minor=700_000),),
            unattributed_revenue_by_currency=(SimpleNamespace(currency="RUB", amount_minor=200_000),),
            limitations=(),
        )
        growth = SimpleNamespace(
            business_id=_BUSINESS,
            period_days=7,
            period_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
            period_to=datetime(2026, 9, 8, tzinfo=timezone.utc),
            period_metrics=(SimpleNamespace(key="leads", value=8, meaning="Новые обращения"),),
            revenue=(SimpleNamespace(currency="RUB", amount_minor=900_000),),
            what_worked=(SimpleNamespace(source=AcquisitionSource.VK.value, label="ВКонтакте", outcomes=4),),
            attention=(),
            actions=(SimpleNamespace(title="Ответить", reason="Есть клиент", action_key="sales_handoff"),),
            advertising=SimpleNamespace(
                connected_accounts=1,
                tracked_ads=2,
                impressions=1000,
                clicks=100,
                leads=8,
                bookings=4,
                won=2,
                ctr_percent=10.0,
                cost_micros=999999999,
                credential_reference="secret://must-not-leak",
            ),
            journey=journey,
            limitations=("advertising_currency_unverified",),
        )
        with (
            patch.object(cockpit_growth_analytics, "resolve_tenant_context", return_value=actor),
            patch.object(cockpit_growth_analytics, "get_growth_cockpit", return_value=growth),
        ):
            snapshot = cockpit_growth_analytics.build_cockpit_growth_analytics(
                actor=actor, business_name="Практика", period_days=7
            )
        rendered = repr(snapshot.as_dict())
        self.assertEqual(snapshot.advertising.ctr_percent, 10.0)
        self.assertNotIn("cost_micros", rendered)
        self.assertNotIn("credential_reference", rendered)
        self.assertNotIn("secret://", rendered)

    def test_services_and_money_have_canonical_telegram_fallback_payloads(self) -> None:
        for section in ("services", "money"):
            payload = build_cockpit_section_start_payload(business_id=_BUSINESS, section=section)
            parsed = parse_cockpit_action_start_payload(payload)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.section, section)
            self.assertEqual(parsed.business_id, _BUSINESS)

    def test_workspace_asset_has_no_browser_authority_or_secret_store(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "clientplatform" / "runtime" / "cockpit_business_workspace.js").read_text(encoding="utf-8")
        transport = (root / "clientplatform" / "runtime" / "cockpit_http.py").read_text(encoding="utf-8")
        for forbidden in (
            "localStorage", "sessionStorage", "URLSearchParams", "innerHTML",
            "credential_reference", "provider_token",
        ):
            self.assertNotIn(forbidden, script)
        self.assertIn("'services','money','growth','analytics'", transport)
        self.assertIn("/clientplatform/cockpit/money/record", script)
        self.assertIn("/clientplatform/cockpit/services/price", script)
        self.assertIn("mutationContext", script)
        self.assertIn("captureBusinessContext", script)
        self.assertIn("assertBusinessContextCurrent", script)
        self.assertIn("servicesRequestId", script)
        self.assertIn("moneyRequestId", script)
        self.assertIn('openExactCanonicalRoute("reactivation"', script)
        self.assertIn('openExactCanonicalRoute("ad-spend"', script)


if __name__ == "__main__":
    unittest.main()
