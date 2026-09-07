from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import cockpit_calendar, cockpit_sales
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_NOW = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)


def _actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=role,
    )


class CockpitPrimaryWorkspaceM7004Tests(unittest.TestCase):
    def test_calendar_projects_only_upcoming_open_and_booked_slots(self) -> None:
        actor = _actor()
        views = [
            SimpleNamespace(
                slot=SimpleNamespace(
                    id="slot-booked",
                    starts_at="2026-09-06T11:00:00+00:00",
                    duration_minutes=60,
                    status="booked",
                ),
                offering_title="Консультация",
                local_start="06.09.2026 14:00",
            ),
            SimpleNamespace(
                slot=SimpleNamespace(
                    id="slot-open",
                    starts_at="2026-09-06T12:30:00+00:00",
                    duration_minutes=45,
                    status="open",
                ),
                offering_title="Диагностика",
                local_start="06.09.2026 15:30",
            ),
            SimpleNamespace(
                slot=SimpleNamespace(
                    id="slot-cancelled",
                    starts_at="2026-09-06T13:00:00+00:00",
                    duration_minutes=30,
                    status="cancelled",
                ),
                offering_title="Не показывать",
                local_start="06.09.2026 16:00",
            ),
            SimpleNamespace(
                slot=SimpleNamespace(
                    id="slot-past",
                    starts_at="2026-09-06T09:00:00+00:00",
                    duration_minutes=30,
                    status="open",
                ),
                offering_title="Прошло",
                local_start="06.09.2026 12:00",
            ),
        ]
        with (
            patch.object(cockpit_calendar, "resolve_tenant_context", return_value=actor),
            patch.object(
                cockpit_calendar,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Tallinn"),
            ),
        ):
            result = cockpit_calendar.build_cockpit_calendar(
                actor=actor,
                business_name="Практика",
                now=_NOW,
                booking_loader=lambda **_kwargs: views,
            )
        self.assertEqual([item.slot_id for item in result.items], ["slot-booked", "slot-open"])
        self.assertEqual([item.status for item in result.items], ["booked", "open"])
        self.assertEqual(result.business_id, _BUSINESS)
        self.assertEqual(result.timezone_name, "Europe/Tallinn")
        self.assertFalse(result.has_more)

    def test_calendar_rechecks_current_permission_before_reading(self) -> None:
        actor = _actor(PlatformRole.MARKETER)
        called = False

        def loader(**_kwargs: object) -> list[object]:
            nonlocal called
            called = True
            return []

        with (
            patch.object(cockpit_calendar, "resolve_tenant_context", return_value=actor),
            patch.object(
                cockpit_calendar,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Tallinn"),
            ),
        ):
            with self.assertRaises(TenantPermissionDenied):
                cockpit_calendar.build_cockpit_calendar(
                    actor=actor,
                    business_name="Практика",
                    now=_NOW,
                    booking_loader=loader,
                )
        self.assertFalse(called)

    def test_invalid_business_timezone_fails_closed_as_projection_unavailable(self) -> None:
        actor = _actor()
        with (
            patch.object(cockpit_calendar, "resolve_tenant_context", return_value=actor),
            patch.object(
                cockpit_calendar,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Invalid/Timezone"),
            ),
        ):
            with self.assertRaises(cockpit_calendar.CockpitCalendarUnavailable):
                cockpit_calendar.build_cockpit_calendar(
                    actor=actor,
                    business_name="Практика",
                    now=_NOW,
                    booking_loader=lambda **_kwargs: [],
                )
        with (
            patch.object(cockpit_sales, "resolve_tenant_context", return_value=actor),
            patch.object(
                cockpit_sales,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Invalid/Timezone"),
            ),
        ):
            with self.assertRaises(cockpit_sales.CockpitSalesUnavailable):
                cockpit_sales.build_cockpit_sales(
                    actor=actor,
                    business_name="Практика",
                    now=_NOW,
                    workspace_loader=lambda **_kwargs: SimpleNamespace(open_work=(), handoff_count=0),
                )

    def test_sales_projects_canonical_queue_and_due_state_without_copying_authority(self) -> None:
        actor = _actor()
        raw = {
            "id": "lead-1",
            "customer_id": "customer-1",
            "customer_name": "  Анна   П. ",
            "stage": "qualified",
            "next_action": "  Позвонить   и подтвердить  ",
            "due_at": "2026-09-06T09:30:00+00:00",
            "source_kind": "vk",
            "attribution_source": "must-not-leak-as-authority",
            "next_plan_id": "internal-plan",
        }
        lost = {
            "id": "lead-lost",
            "customer_id": "customer-lost",
            "customer_name": "Борис",
            "stage": "lost",
            "source_kind": "telegram",
        }
        won = {
            "id": "lead-won",
            "customer_id": "customer-won",
            "customer_name": "Виктор",
            "stage": "won",
            "source_kind": "vk",
        }
        workspace = SimpleNamespace(
            open_work=(raw,),
            handoff_work=(),
            recent_closed=(won, lost),
            handoff_count=2,
        )
        with (
            patch.object(cockpit_sales, "resolve_tenant_context", return_value=actor),
            patch.object(
                cockpit_sales,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Tallinn"),
            ),
        ):
            result = cockpit_sales.build_cockpit_sales(
                actor=actor,
                business_name="Практика",
                now=_NOW,
                workspace_loader=lambda **_kwargs: workspace,
            )
        self.assertEqual(result.handoff_count, 2)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(len(result.recent_lost), 1)
        self.assertEqual(result.recent_lost[0].lead_id, "lead-lost")
        self.assertEqual(result.recent_lost[0].stage_label, "Не состоялось")
        item = result.items[0]
        self.assertEqual(item.customer_name, "Анна П.")
        self.assertEqual(item.stage_label, "Интерес подтверждён")
        self.assertEqual(item.next_action, "Позвонить и подтвердить")
        self.assertTrue(item.overdue)
        self.assertEqual(item.due_display, "06.09 12:30")
        payload = result.as_dict()["items"][0]
        self.assertNotIn("attribution_source", payload)
        self.assertNotIn("next_plan_id", payload)

    def test_primary_mobile_assets_are_first_party_and_have_no_client_side_authority_store(self) -> None:
        root = Path(__file__).resolve().parents[1]
        transport = (root / "clientplatform" / "runtime" / "cockpit_http.py").read_text(encoding="utf-8")
        calendar = (root / "clientplatform" / "runtime" / "cockpit_calendar.js").read_text(encoding="utf-8")
        sales = (root / "clientplatform" / "runtime" / "cockpit_sales.js").read_text(encoding="utf-8")
        customers = (root / "clientplatform" / "runtime" / "cockpit_customers.js").read_text(encoding="utf-8")

        self.assertIn('id="primary-nav"', transport)
        self.assertIn('data-primary="home"', transport)
        self.assertIn('data-primary="customers"', transport)
        self.assertIn('data-primary="calendar"', transport)
        self.assertIn('data-primary="sales"', transport)
        self.assertIn("Что сделать сейчас", transport)
        self.assertIn("/clientplatform/cockpit/calendar", calendar)
        self.assertIn("/clientplatform/cockpit/sales", sales)
        self.assertIn("openCustomer(customerId, \"sales\")", sales)
        self.assertIn("openCustomer", customers)
        self.assertIn("detailReturn === 'sales'", customers)
        for script in (calendar, sales, customers):
            self.assertNotIn("localStorage", script)
            self.assertNotIn("sessionStorage", script)
            self.assertNotIn("URLSearchParams", script)
            self.assertNotIn("innerHTML", script)
            self.assertNotIn("/approve", script)
            self.assertNotIn("/send", script)


if __name__ == "__main__":
    unittest.main()
