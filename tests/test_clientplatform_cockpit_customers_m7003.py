from __future__ import annotations

import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from clientplatform.application import cockpit_customers
from clientplatform.application.customer_timeline import (
    CustomerTimeline,
    CustomerTimelineEntry,
)
from clientplatform.application.growth_cockpit import GrowthAction
from clientplatform.domain.customers import (
    Customer,
    CustomerIdentity,
    CustomerIdentityStatus,
    CustomerPlatform,
    CustomerRecord,
    CustomerStatus,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "22222222-2222-4222-8222-222222222222"
_CUSTOMER = "33333333-3333-4333-8333-333333333333"


def _actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=role,
    )


def _customer(name: str = "Анна") -> Customer:
    return Customer(
        id=_CUSTOMER,
        business_id=_BUSINESS,
        display_name=name,
        status=CustomerStatus.ACTIVE,
        created_by_member_id=_MEMBER,
        created_at="2026-09-01T10:00:00+00:00",
        updated_at="2026-09-05T10:00:00+00:00",
    )


class CockpitCustomersM7003Tests(unittest.TestCase):
    def test_page_keeps_bounded_pagination_contract(self) -> None:
        calls: list[tuple[str, int, int]] = []

        def load(*, actor: TenantContext, query: str, limit: int, offset: int):
            self.assertEqual(actor.business_id, _BUSINESS)
            calls.append((query, limit, offset))
            return [_customer()], True

        page = cockpit_customers.build_cockpit_customer_page(
            actor=_actor(), query="  Анна  ", limit=10, offset=20, loader=load
        )

        self.assertEqual(calls, [("Анна", 10, 20)])
        self.assertEqual(page.next_offset, 30)
        self.assertEqual(page.previous_offset, 10)
        self.assertEqual(page.items[0].customer_id, _CUSTOMER)
        self.assertNotIn("external_subject", repr(page.as_dict()))

    def test_detail_masks_provider_subjects_preserves_timeline_and_routes_action(self) -> None:
        phone = CustomerIdentity(
            id="44444444-4444-4444-8444-444444444444",
            business_id=_BUSINESS,
            customer_id=_CUSTOMER,
            platform=CustomerPlatform.PHONE,
            external_subject="79991234567",
            username=None,
            display_name=None,
            status=CustomerIdentityStatus.ACTIVE,
            created_at="2026-09-01T10:00:00+00:00",
            updated_at="2026-09-01T10:00:00+00:00",
        )
        email = CustomerIdentity(
            id="55555555-5555-4555-8555-555555555555",
            business_id=_BUSINESS,
            customer_id=_CUSTOMER,
            platform=CustomerPlatform.EMAIL,
            external_subject="private.person@example.com",
            username=None,
            display_name=None,
            status=CustomerIdentityStatus.ACTIVE,
            created_at="2026-09-01T10:00:00+00:00",
            updated_at="2026-09-01T10:00:00+00:00",
        )
        record = CustomerRecord(customer=_customer(), identities=(phone, email))
        timeline = CustomerTimeline(
            business_id=_BUSINESS,
            customer_id=_CUSTOMER,
            entries=(
                CustomerTimelineEntry(
                    kind="customer:created",
                    occurred_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                    source_type="customer",
                    source_id=_CUSTOMER,
                    title="Клиент добавлен",
                ),
                CustomerTimelineEntry(
                    kind="outcome:order_paid",
                    occurred_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
                    source_type="outcome_event",
                    source_id="66666666-6666-4666-8666-666666666666",
                    title="Получена оплата",
                    amount_minor=50000,
                    currency="RUB",
                ),
            ),
        )
        action = GrowthAction(
            title="Ответить лично: Анна",
            reason="Клиенту требуется личное участие.",
            action_key="sales_handoff",
            source="sales_handoff_queue",
        )
        detail = cockpit_customers.build_cockpit_customer_detail(
            actor=_actor(),
            customer_id=_CUSTOMER,
            record_loader=lambda **_: record,
            timeline_loader=lambda **_: timeline,
            action_loader=lambda **_: (action,),
        )

        payload = detail.as_dict()
        self.assertEqual(
            [item["title"] for item in payload["timeline"]],
            ["Клиент добавлен", "Получена оплата"],
        )
        self.assertEqual(payload["timeline"][1]["money"], "500,00 RUB")
        self.assertEqual(payload["contacts"][0]["display"], "•••• 4567")
        self.assertEqual(payload["contacts"][1]["display"], "•••@example.com")
        self.assertEqual(payload["next_action"]["section"], "sales")
        rendered = repr(payload)
        self.assertNotIn("79991234567", rendered)
        self.assertNotIn("private.person@example.com", rendered)

    def test_optional_timeline_or_action_failure_does_not_invent_data(self) -> None:
        record = CustomerRecord(customer=_customer(), identities=())

        def broken(**_: object):
            raise RuntimeError("optional source unavailable")

        detail = cockpit_customers.build_cockpit_customer_detail(
            actor=_actor(),
            customer_id=_CUSTOMER,
            record_loader=lambda **_: record,
            timeline_loader=broken,
            action_loader=broken,
        )

        self.assertEqual(detail.timeline, ())
        self.assertIsNone(detail.next_action)
        self.assertEqual(
            detail.limitations,
            ("timeline_unavailable", "customer_work_unavailable"),
        )

    def test_mobile_asset_is_read_only_and_keeps_authority_on_server(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (
            root / "clientplatform" / "runtime" / "cockpit_customers.js"
        ).read_text(encoding="utf-8")
        transport = (
            root / "clientplatform" / "runtime" / "cockpit_http.py"
        ).read_text(encoding="utf-8")
        self.assertIn("/clientplatform/cockpit/customers", script)
        self.assertIn("/clientplatform/cockpit/customers/detail", script)
        self.assertIn("/clientplatform/cockpit/customers/action-route", script)
        self.assertIn("openTelegramLink", script)
        self.assertIn("ClientPlatformCockpitNavigation", script)
        self.assertIn("enterCustomers", script)
        self.assertIn("back:handleBack", script)
        self.assertIn('id=\"customers-view\"', transport)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("URLSearchParams", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("/approve", script)
        self.assertNotIn("/send", script)
        refresh_handler = script.split("refresh.addEventListener", 1)[1].split(
            "searchForm.addEventListener", 1
        )[0]
        self.assertIn("loadPage", refresh_handler)
        self.assertNotIn("action-route", refresh_handler)

    def test_resolver_rechecks_current_tenant_after_cockpit_scope(self) -> None:
        context = type(
            "Context",
            (),
            {
                "onboarding_required": False,
                "business_id": _BUSINESS,
                "user_id": 101,
            },
        )()
        actor = _actor(PlatformRole.SUPPORT)
        with (
            patch.object(cockpit_customers, "resolve_cockpit_context", return_value=context),
            patch.object(cockpit_customers, "resolve_tenant_context", return_value=actor) as live,
            patch.object(
                cockpit_customers,
                "build_cockpit_customer_page",
                return_value=cockpit_customers.CockpitCustomerPage(
                    schema_version="2026-09-05.v1",
                    business_id=_BUSINESS,
                    role="support",
                    query="",
                    limit=20,
                    offset=0,
                    has_more=False,
                    next_offset=None,
                    previous_offset=None,
                    items=(),
                ),
            ),
        ):
            result = cockpit_customers.resolve_cockpit_customer_page(
                telegram_user_id=101, requested_business_id=_BUSINESS
            )
        self.assertEqual(result.role, "support")
        live.assert_called_once_with(user_id=101, business_id=_BUSINESS)


if __name__ == "__main__":
    unittest.main()
