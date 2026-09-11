from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from clientplatform.application import cockpit_sales_management as management
from clientplatform.domain.sales import SalesInvariantViolation
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_LEAD = "22222222-2222-4222-8222-222222222222"
_CUSTOMER = "33333333-3333-4333-8333-333333333333"


def _actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=role,
    )


def _item(
    *,
    stage: str = "qualified",
    assigned_user_id: int | None = 101,
    due_at: str | None = "2026-09-10T12:30:00+00:00",
) -> dict[str, object]:
    return {
        "id": _LEAD,
        "customer_id": _CUSTOMER,
        "customer_name": "  Анна   Иванова ",
        "source_kind": "vk",
        "stage": stage,
        "assigned_user_id": assigned_user_id,
        "next_action": None if stage in {"won", "lost"} else "  Позвонить   после встречи ",
        "due_at": None if stage in {"won", "lost"} else due_at,
        "closure_reason": "другой вариант" if stage == "lost" else None,
    }


class CockpitSalesManagementM7007Tests(unittest.TestCase):
    def test_detail_projects_only_safe_management_state(self) -> None:
        actor = _actor()
        with patch.object(management, "get_sales_workspace_item", return_value=_item()):
            detail = management._management_item(
                actor=actor,
                business_name="Практика",
                zone=ZoneInfo("Europe/Moscow"),
                lead_id=_LEAD,
            )
        self.assertEqual(detail.business_id, _BUSINESS)
        self.assertEqual(detail.customer_name, "Анна Иванова")
        self.assertEqual(detail.stage_label, "Интерес подтверждён")
        self.assertTrue(detail.assigned)
        self.assertTrue(detail.assigned_to_me)
        self.assertEqual(detail.due_local_value, "2026-09-10T15:30")
        self.assertFalse(detail.closed)
        payload = detail.as_dict()
        self.assertNotIn("assigned_member_id", payload)
        self.assertNotIn("assigned_user_id", payload)
        self.assertNotIn("source_ref", payload)

    def test_closed_projection_allows_only_lost_reopen_signal(self) -> None:
        actor = _actor()
        with patch.object(management, "get_sales_workspace_item", return_value=_item(stage="lost")):
            lost = management._management_item(
                actor=actor,
                business_name="Практика",
                zone=ZoneInfo("Europe/Moscow"),
                lead_id=_LEAD,
            )
        self.assertTrue(lost.closed)
        self.assertTrue(lost.can_reopen)
        self.assertEqual(lost.closure_reason, "другой вариант")
        with patch.object(management, "get_sales_workspace_item", return_value=_item(stage="won")):
            won = management._management_item(
                actor=actor,
                business_name="Практика",
                zone=ZoneInfo("Europe/Moscow"),
                lead_id=_LEAD,
            )
        self.assertTrue(won.closed)
        self.assertFalse(won.can_reopen)

    def test_business_local_due_is_dst_safe(self) -> None:
        zone = ZoneInfo("Europe/Berlin")
        self.assertEqual(
            management._local_due_to_utc("2026-09-10T15:30", zone=zone),
            "2026-09-10T13:30:00+00:00",
        )
        with self.assertRaisesRegex(ValueError, "does not exist"):
            management._local_due_to_utc("2026-03-29T02:30", zone=zone)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            management._local_due_to_utc("2026-10-25T02:30", zone=zone)
        for invalid in ("2026-09-10", "2026-09-10T15:30:45", "2026-09-10T15:30+03:00"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "must look like"):
                    management._local_due_to_utc(invalid, zone=zone)

    def test_manager_permission_is_rechecked_from_current_tenant(self) -> None:
        support = _actor(PlatformRole.SUPPORT)
        with patch.object(management, "resolve_tenant_context", return_value=support):
            self.assertEqual(management._current_sales_manager(support), support)
        analyst = _actor(PlatformRole.ANALYST)
        with patch.object(management, "resolve_tenant_context", return_value=analyst):
            with self.assertRaises(TenantPermissionDenied):
                management._current_sales_manager(analyst)

    def test_assignment_and_unassignment_delegate_to_canonical_workspace(self) -> None:
        actor = _actor()
        zone = ZoneInfo("Europe/Moscow")
        before = _item(assigned_user_id=None)
        after = _item(assigned_user_id=101)
        with (
            patch.object(management, "_resolve_manager", return_value=(actor, "Практика", zone)),
            patch.object(management, "get_sales_workspace_item", side_effect=[before, after]),
            patch.object(management, "assign_sales_workspace_to_actor") as assign,
        ):
            result = management.assign_cockpit_sales_lead(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                lead_id=_LEAD,
            )
        assign.assert_called_once_with(actor=actor, lead_id=_LEAD)
        self.assertTrue(result.assigned_to_me)

        with (
            patch.object(management, "_resolve_manager", return_value=(actor, "Практика", zone)),
            patch.object(management, "get_sales_workspace_item", side_effect=[after, before]),
            patch.object(management, "unassign_sales_workspace") as unassign,
        ):
            result = management.unassign_cockpit_sales_lead(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                lead_id=_LEAD,
            )
        unassign.assert_called_once_with(actor=actor, lead_id=_LEAD)
        self.assertFalse(result.assigned)

    def test_closed_lead_cannot_be_assigned_through_cockpit_adapter(self) -> None:
        actor = _actor()
        with (
            patch.object(management, "_resolve_manager", return_value=(actor, "Практика", ZoneInfo("UTC"))),
            patch.object(management, "get_sales_workspace_item", return_value=_item(stage="won")),
            patch.object(management, "assign_sales_workspace_to_actor") as assign,
        ):
            with self.assertRaises(SalesInvariantViolation):
                management.assign_cockpit_sales_lead(
                    telegram_user_id=101,
                    requested_business_id=_BUSINESS,
                    lead_id=_LEAD,
                )
        assign.assert_not_called()

    def test_stage_close_requires_reason_and_uses_canonical_transition(self) -> None:
        actor = _actor()
        zone = ZoneInfo("UTC")
        with patch.object(management, "_resolve_manager", return_value=(actor, "Практика", zone)):
            with self.assertRaisesRegex(ValueError, "requires a reason"):
                management.set_cockpit_sales_stage(
                    telegram_user_id=101,
                    requested_business_id=_BUSINESS,
                    lead_id=_LEAD,
                    stage="lost",
                    reason="",
                )
        with (
            patch.object(management, "_resolve_manager", return_value=(actor, "Практика", zone)),
            patch.object(management, "transition_sales_workspace") as transition,
            patch.object(management, "get_sales_workspace_item", return_value=_item(stage="lost")),
        ):
            result = management.set_cockpit_sales_stage(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                lead_id=_LEAD,
                stage="lost",
                reason="Клиент выбрал другой вариант",
            )
        self.assertTrue(result.closed)
        transition.assert_called_once()
        call = transition.call_args.kwargs
        self.assertEqual(call["actor"], actor)
        self.assertEqual(call["lead_id"], _LEAD)
        self.assertEqual(call["stage"].value, "lost")
        self.assertEqual(call["reason"], "Клиент выбрал другой вариант")

    def test_next_action_converts_business_local_time_and_can_clear(self) -> None:
        actor = _actor()
        zone = ZoneInfo("Europe/Moscow")
        open_item = _item(due_at=None)
        with (
            patch.object(management, "_resolve_manager", return_value=(actor, "Практика", zone)),
            patch.object(management, "get_sales_workspace_item", side_effect=[open_item, open_item]),
            patch.object(management, "set_sales_workspace_next_action") as set_action,
        ):
            management.set_cockpit_sales_next_action(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                lead_id=_LEAD,
                next_action="Отправить предложение",
                due_local="2026-09-10T15:30",
            )
        set_action.assert_called_once_with(
            actor=actor,
            lead_id=_LEAD,
            next_action="Отправить предложение",
            due_at="2026-09-10T12:30:00+00:00",
        )

        with (
            patch.object(management, "_resolve_manager", return_value=(actor, "Практика", zone)),
            patch.object(management, "get_sales_workspace_item", side_effect=[open_item, open_item]),
            patch.object(management, "set_sales_workspace_next_action") as clear,
        ):
            management.set_cockpit_sales_next_action(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                lead_id=_LEAD,
                next_action=None,
                due_local=None,
            )
        clear.assert_called_once_with(actor=actor, lead_id=_LEAD, next_action=None, due_at=None)

    def test_note_prefixes_client_idempotency_key_with_server_membership(self) -> None:
        actor = _actor()
        with (
            patch.object(management, "_resolve_manager", return_value=(actor, "Практика", ZoneInfo("UTC"))),
            patch.object(management, "add_sales_workspace_note") as add_note,
            patch.object(management, "get_sales_workspace_item", return_value=_item()),
        ):
            management.add_cockpit_sales_note(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                lead_id=_LEAD,
                note="Клиент просит написать завтра",
                interaction_key="browser-event-1",
            )
        add_note.assert_called_once_with(
            actor=actor,
            lead_id=_LEAD,
            note="Клиент просит написать завтра",
            interaction_key=f"cockpit:{_MEMBER}:browser-event-1",
        )

    def test_reopen_is_lost_only(self) -> None:
        actor = _actor()
        zone = ZoneInfo("UTC")
        with (
            patch.object(management, "_resolve_manager", return_value=(actor, "Практика", zone)),
            patch.object(management, "get_sales_workspace_item", return_value=_item(stage="won")),
            patch.object(management, "reopen_sales_workspace") as reopen,
        ):
            with self.assertRaises(SalesInvariantViolation):
                management.reopen_cockpit_sales_lead(
                    telegram_user_id=101,
                    requested_business_id=_BUSINESS,
                    lead_id=_LEAD,
                )
        reopen.assert_not_called()

        with (
            patch.object(management, "_resolve_manager", return_value=(actor, "Практика", zone)),
            patch.object(management, "get_sales_workspace_item", side_effect=[_item(stage="lost"), _item(stage="new")]),
            patch.object(management, "reopen_sales_workspace") as reopen,
        ):
            result = management.reopen_cockpit_sales_lead(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                lead_id=_LEAD,
            )
        self.assertFalse(result.closed)
        reopen.assert_called_once_with(actor=actor, lead_id=_LEAD, reason="reopened_from_cockpit")

    def test_native_sales_surface_preserves_fallback_and_has_no_browser_authority(self) -> None:
        sales_js = Path("clientplatform/runtime/cockpit_sales.js").read_text(encoding="utf-8")
        shell = Path("clientplatform/runtime/cockpit_http.py").read_text(encoding="utf-8")
        for endpoint in (
            "/clientplatform/cockpit/sales/manage",
            "/clientplatform/cockpit/sales/assignment",
            "/clientplatform/cockpit/sales/stage",
            "/clientplatform/cockpit/sales/reopen",
            "/clientplatform/cockpit/sales/next-action",
            "/clientplatform/cockpit/sales/note",
        ):
            self.assertIn(endpoint, sales_js)
            self.assertIn(endpoint.removeprefix("/clientplatform/cockpit/"), shell)
        self.assertIn("Продолжить в Telegram", shell)
        self.assertNotIn("Все возможности раздела", shell)
        self.assertIn("Открыть карточку клиента", shell)
        self.assertIn('openCanonicalSection("sales"', sales_js)
        self.assertIn('openCustomer(activeCustomerId)', sales_js)
        self.assertIn('assignment.dataset.action', sales_js)
        self.assertIn('payload.recent_lost', sales_js)
        self.assertIn('Недавно не состоялись', sales_js)
        for forbidden in ("localStorage", "sessionStorage", "innerHTML"):
            self.assertNotIn(forbidden, sales_js)
        self.assertNotIn("assigned_member_id", sales_js)
        self.assertNotIn("assigned_user_id", sales_js)
        self.assertNotIn("due_at:", sales_js)
        self.assertNotIn("timezone:", sales_js)


if __name__ == "__main__":
    unittest.main()
