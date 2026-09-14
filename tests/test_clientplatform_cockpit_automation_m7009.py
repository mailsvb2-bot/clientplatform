from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import cockpit_automation
from clientplatform.domain.automation_policy import (
    AutomationApprovalConflict,
    AutomationApprovalStatus,
    AutomationMode,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_APPROVAL = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_FINGERPRINT = "a" * 64
_NOW = datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc)


def _actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=role,
    )


def _approval(status: AutomationApprovalStatus = AutomationApprovalStatus.PENDING):
    return SimpleNamespace(
        id=_APPROVAL,
        request_fingerprint=_FINGERPRINT,
        status=status,
        candidate=SimpleNamespace(
            action="sales.followup",
            channel="telegram",
            amount_minor=None,
            currency=None,
            subject_ref="customer:opaque-internal-ref",
            payload_digest="b" * 64,
        ),
        approval_reasons=("action_requires_approval",),
        requested_at="2026-09-14T15:00:00+00:00",
        expires_at="2026-09-15T15:00:00+00:00",
    )


class CockpitAutomationM7009Tests(unittest.TestCase):
    def _build(self, role: PlatformRole, status: AutomationApprovalStatus):
        actor = _actor(role)
        effective = SimpleNamespace(
            spec=SimpleNamespace(
                mode=AutomationMode.NORMAL,
                expires_at="2026-10-01T12:00:00+00:00",
            )
        )
        with (
            patch.object(cockpit_automation, "resolve_tenant_context", return_value=actor),
            patch.object(
                cockpit_automation,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Moscow"),
            ),
            patch.object(cockpit_automation, "get_effective_automation_policy", return_value=effective),
            patch.object(cockpit_automation, "get_latest_automation_policy", return_value=effective),
            patch.object(
                cockpit_automation,
                "list_current_automation_action_approvals",
                return_value=(_approval(status),),
            ),
            patch.object(cockpit_automation, "is_owner_autopilot_enabled", return_value=False),
        ):
            return cockpit_automation.build_cockpit_automation(
                actor=actor,
                business_name="Практика",
                now=_NOW,
            )

    def test_owner_projection_is_human_safe_and_decision_capable(self) -> None:
        snapshot = self._build(PlatformRole.OWNER, AutomationApprovalStatus.PENDING)
        self.assertEqual(snapshot.business_id, _BUSINESS)
        self.assertEqual(snapshot.mode_label, "Обычный")
        self.assertEqual(snapshot.pending_count, 1)
        self.assertTrue(snapshot.can_change_policy)
        item = snapshot.approvals[0]
        self.assertEqual(item.title, "Связаться с клиентом")
        self.assertEqual(item.channel_label, "Telegram")
        self.assertTrue(item.can_approve)
        self.assertTrue(item.can_reject)
        rendered = repr(snapshot.as_dict())
        self.assertNotIn("customer:opaque-internal-ref", rendered)
        self.assertNotIn("b" * 64, rendered)
        self.assertNotIn("sales.followup", rendered)

    def test_inactive_autopilot_draft_never_looks_effective(self) -> None:
        actor = _actor()
        latest = SimpleNamespace(spec=SimpleNamespace(mode=AutomationMode.AUTOPILOT))
        with (
            patch.object(cockpit_automation, "resolve_tenant_context", return_value=actor),
            patch.object(
                cockpit_automation,
                "get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Moscow"),
            ),
            patch.object(cockpit_automation, "get_effective_automation_policy", return_value=None),
            patch.object(cockpit_automation, "get_latest_automation_policy", return_value=latest),
            patch.object(cockpit_automation, "list_current_automation_action_approvals", return_value=()),
            patch.object(cockpit_automation, "is_owner_autopilot_enabled", return_value=False),
        ):
            snapshot = cockpit_automation.build_cockpit_automation(
                actor=actor, business_name="Практика", now=_NOW
            )
        self.assertEqual(snapshot.mode, "cautious")
        self.assertEqual(snapshot.mode_label, "Осторожный")
        self.assertIn("не разрешены", snapshot.policy_state)
        self.assertFalse(snapshot.autopilot_enabled)

    def test_administrator_can_read_but_cannot_change_owner_decisions(self) -> None:
        snapshot = self._build(PlatformRole.ADMINISTRATOR, AutomationApprovalStatus.APPROVED)
        self.assertFalse(snapshot.can_change_policy)
        item = snapshot.approvals[0]
        self.assertFalse(item.can_approve)
        self.assertFalse(item.can_reject)
        self.assertFalse(item.can_revoke)
        self.assertEqual(item.status_label, "Разрешено владельцем")

    def test_stale_browser_fingerprint_fails_before_any_decision(self) -> None:
        actor = _actor()
        approval = _approval()
        with (
            patch.object(cockpit_automation, "_resolve_actor", return_value=(actor, "Практика")),
            patch.object(cockpit_automation, "get_automation_action_approval", return_value=approval),
            patch.object(cockpit_automation, "approve_automation_action") as approve,
            patch.object(cockpit_automation, "reject_automation_action") as reject,
            patch.object(cockpit_automation, "revoke_automation_action_approval") as revoke,
        ):
            with self.assertRaises(AutomationApprovalConflict):
                cockpit_automation.decide_cockpit_automation_approval(
                    telegram_user_id=101,
                    requested_business_id=_BUSINESS,
                    approval_id=_APPROVAL,
                    request_fingerprint="c" * 64,
                    decision="approve",
                )
        approve.assert_not_called()
        reject.assert_not_called()
        revoke.assert_not_called()

    def test_exact_decision_delegates_to_canonical_approval_owner(self) -> None:
        actor = _actor()
        approval = _approval()
        refreshed = SimpleNamespace(as_dict=lambda: {})
        with (
            patch.object(cockpit_automation, "_resolve_actor", return_value=(actor, "Практика")),
            patch.object(cockpit_automation, "get_automation_action_approval", return_value=approval),
            patch.object(cockpit_automation, "approve_automation_action") as approve,
            patch.object(cockpit_automation, "build_cockpit_automation", return_value=refreshed),
        ):
            result = cockpit_automation.decide_cockpit_automation_approval(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                approval_id=_APPROVAL,
                request_fingerprint=_FINGERPRINT,
                decision="approve",
            )
        self.assertIs(result, refreshed)
        approve.assert_called_once_with(
            actor=actor,
            approval_id=_APPROVAL,
            expected_request_fingerprint=_FINGERPRINT,
        )

    def test_native_script_has_no_browser_authority_or_second_policy_store(self) -> None:
        script = (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "clientplatform"
            / "runtime"
            / "cockpit_automation.js"
        ).read_text(encoding="utf-8")
        self.assertIn("/clientplatform/cockpit/automation/decision", script)
        self.assertIn("request_fingerprint", script)
        for forbidden in (
            "localStorage",
            "sessionStorage",
            "URLSearchParams",
            "innerHTML",
            "subject_ref",
            "payload_digest",
            "policy_hash",
        ):
            self.assertNotIn(forbidden, script)


if __name__ == "__main__":
    unittest.main()
