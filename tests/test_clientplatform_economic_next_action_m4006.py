from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application.growth_cockpit import (
    GrowthAction,
    _action_queue,
    _economic_next_action,
)
from clientplatform.application import native_member_interactions as native
from clientplatform.domain.ad_spend import AdSpendAuthorizationStatus
from clientplatform.domain.tenancy import PlatformRole, TenantContext

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
AIOGRAM_AVAILABLE = importlib.util.find_spec("aiogram") is not None


def journey(*, reactivated: int = 0, yandex_paid: int = 0):
    return SimpleNamespace(
        reactivated_customers=reactivated,
        sources=(
            SimpleNamespace(
                source=SimpleNamespace(value="yandex_direct"),
                paid_customers=yandex_paid,
            ),
        ),
    )


def opportunity(*, route: str | None = "vk", customer_id: str | None = None):
    return SimpleNamespace(
        route_platform=route,
        candidate=SimpleNamespace(customer_id=customer_id or str(uuid4())),
    )


def authorization(
    *,
    status: AdSpendAuthorizationStatus = AdSpendAuthorizationStatus.AUTHORIZED,
    consent: bool = True,
    expires_delta: timedelta = timedelta(hours=2),
):
    return SimpleNamespace(
        id=str(uuid4()),
        status=status,
        consent_receipt=object() if consent else None,
        authorization_expires_at=(NOW + expires_delta).isoformat(),
        snapshot=SimpleNamespace(valid_until=(NOW + expires_delta).isoformat()),
        hard_cap_minor=50_000_00,
        daily_cap_minor=10_000_00,
        currency="RUB",
    )


class EconomicNextBestActionM4006Tests(unittest.TestCase):
    def test_capacity_is_checked_before_any_growth_spend_or_reactivation(self) -> None:
        action = _economic_next_action(
            open_slots=0,
            reactivation=[opportunity()],
            authorizations=[authorization()],
            journey=journey(reactivated=3, yandex_paid=2),
            now=NOW,
        )
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_key, "economic_open_slots")
        self.assertEqual(action.source, "booking_availability")
        self.assertIn("не предлагает тратить деньги", action.reason)

    def test_free_reactivation_wins_over_owner_consented_paid_acquisition(self) -> None:
        customer_id = str(uuid4())
        action = _economic_next_action(
            open_slots=4,
            reactivation=[opportunity(route="max", customer_id=customer_id)],
            authorizations=[authorization()],
            journey=journey(reactivated=2, yandex_paid=5),
            now=NOW,
        )
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_key, "economic_reactivation")
        self.assertEqual(action.source_id, customer_id)
        self.assertIn("без рекламных расходов", action.reason)
        self.assertIn("сообщение само не отправится", action.reason.casefold())

    def test_paid_acquisition_requires_current_consent_bound_authorization(self) -> None:
        for item in (
            authorization(consent=False),
            authorization(status=AdSpendAuthorizationStatus.REVOKED),
            authorization(expires_delta=timedelta(seconds=-1)),
        ):
            with self.subTest(status=item.status, consent=item.consent_receipt is not None):
                action = _economic_next_action(
                    open_slots=2,
                    reactivation=[opportunity(route=None)],
                    authorizations=[item],
                    journey=journey(yandex_paid=1),
                    now=NOW,
                )
                self.assertIsNone(action)

        action = _economic_next_action(
            open_slots=2,
            reactivation=[],
            authorizations=[authorization()],
            journey=journey(yandex_paid=2),
            now=NOW,
        )
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_key, "economic_paid_acquisition")
        self.assertIn("50 000.00 RUB", action.reason)
        self.assertIn("10 000.00 RUB", action.reason)
        self.assertIn("consent-bound", action.reason)

    def test_existing_owner_work_stays_ahead_of_economic_recommendation(self) -> None:
        economic = GrowthAction(
            title="Вернуть клиента",
            reason="free first",
            action_key="economic_reactivation",
            source="retention_projection",
        )
        economics = SimpleNamespace(unattributed_monetary_outcomes=0)
        actions = _action_queue(
            handoffs=[
                {
                    "lead_id": "lead-1",
                    "id": "handoff-1",
                    "customer_name": "Анна",
                    "severity": "urgent",
                }
            ],
            sales_work=[],
            economics=economics,
            economic_action=economic,
        )
        self.assertEqual(actions[0].action_key, "sales_handoff")
        self.assertEqual(actions[1].action_key, "economic_reactivation")


class NativeEconomicActionParityM4006Tests(unittest.TestCase):
    @staticmethod
    def actor() -> TenantContext:
        return TenantContext(
            business_id=str(uuid4()),
            membership_id=str(uuid4()),
            user_id=70001,
            role=PlatformRole.OWNER,
        )

    def test_native_primary_buttons_route_to_existing_safe_workflows(self) -> None:
        actor = self.actor()
        cases = (
            ("economic_reactivation", "cpm:reactivate"),
            ("economic_open_slots", "cpm:acquire"),
            ("economic_paid_acquisition", "cpm:ad-spend"),
        )
        for key, command in cases:
            with self.subTest(key=key):
                button = native._native_growth_action_button(
                    actor,
                    GrowthAction(title="x", reason="y", action_key=key, source="test"),
                )
                self.assertIsNotNone(button)
                assert button is not None
                self.assertEqual(button.command, command)

    def test_native_paid_action_reuses_exact_safe_launch_boundary(self) -> None:
        actor = self.actor()
        authorization_id = str(uuid4())
        item = SimpleNamespace(
            id=authorization_id,
            status=AdSpendAuthorizationStatus.AUTHORIZED,
            hard_cap_minor=50_000_00,
            daily_cap_minor=10_000_00,
            currency="RUB",
        )
        with (
            patch.object(native, "list_ad_spend_authorizations", return_value=[item]),
            patch.object(native, "ad_spend_mutations_enabled", return_value=True),
        ):
            message = native._ad_spend_message(actor)
        commands = [button.command for row in message.rows for button in row]
        self.assertIn(f"cpm:ad-spend-launch:{authorization_id}", commands)
        self.assertIn("50 000.00 RUB", message.text)
        self.assertIn("10 000.00 RUB", message.text)

        operation_id = str(uuid4())
        with patch.object(
            native,
            "queue_ad_spend_launch",
            return_value=SimpleNamespace(id=operation_id),
        ) as queue:
            result = native._ad_spend_launch_message(actor, authorization_id)
        queue.assert_called_once_with(actor=actor, authorization_id=authorization_id)
        self.assertIn("идемпотентную очередь", result.text)
        self.assertIn(operation_id[-12:], result.text)

    def test_native_reactivation_review_is_read_then_explicit_materialization(self) -> None:
        actor = self.actor()
        customer_id = str(uuid4())
        candidate = SimpleNamespace(
            customer_id=customer_id,
            display_name="Анна",
            inactive_days=95,
            cohort=SimpleNamespace(value="inactive_customer"),
        )
        with patch.object(
            native,
            "list_reactivation_opportunities",
            return_value=[SimpleNamespace(candidate=candidate, route_platform="vk")],
        ):
            message = native._reactivation_message(actor)
        commands = [button.command for row in message.rows for button in row]
        self.assertIn(
            f"cpm:reactivate-approve:{customer_id}:inactive_customer",
            commands,
        )
        self.assertIn("Ничего не отправляется автоматически", message.text)

        lead_id = str(uuid4())
        prepared = SimpleNamespace(lead=SimpleNamespace(id=lead_id), route_platform="vk")
        with patch.object(native, "prepare_reactivation_sales_lead", return_value=prepared) as prepare:
            result = native._reactivation_approve_message(actor, customer_id, "inactive_customer")
        prepare.assert_called_once()
        self.assertIn("ничего не отправлено", result.text.casefold())
        self.assertEqual(result.rows[0][0].command, f"cpm:sales-lead:{lead_id}")


@unittest.skipUnless(AIOGRAM_AVAILABLE, "aiogram is not installed")
class TelegramEconomicActionParityM4006Tests(unittest.TestCase):
    def test_growth_keyboard_routes_economic_actions_to_canonical_flows(self) -> None:
        from handlers.clientplatform_growth import _cockpit_keyboard

        business_id = str(uuid4())
        reactivation = _cockpit_keyboard(
            business_id=business_id,
            period_days=7,
            action_key="economic_reactivation",
        )
        acquisition = _cockpit_keyboard(
            business_id=business_id,
            period_days=7,
            action_key="economic_paid_acquisition",
        )
        reactivation_callbacks = [
            str(button.callback_data) for row in reactivation.inline_keyboard for button in row
        ]
        acquisition_callbacks = [
            str(button.callback_data) for row in acquisition.inline_keyboard for button in row
        ]
        self.assertTrue(any(value.startswith("cps:sr:") for value in reactivation_callbacks))
        self.assertTrue(any(value.startswith("cpsp:home:") for value in acquisition_callbacks))


if __name__ == "__main__":
    unittest.main()
