from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.application.growth_cockpit import get_growth_cockpit
from clientplatform.domain.attribution import AcquisitionSource
from clientplatform.domain.revenue_attribution import (
    MoneyBreakdown,
    RevenueAttributionModel,
    RevenueJourneySnapshot,
    RevenueJourneySourceResult,
    UnitEconomicsSnapshot,
)
from clientplatform.domain.tenancy import TenantContext
from dashboard.growth_cockpit import growth_cockpit_payload, telegram_growth_summary


_AIOGRAM_AVAILABLE = importlib.util.find_spec("aiogram") is not None
_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
_ACTOR = TenantContext(
    user_id=7001,
    business_id="11111111-1111-4111-8111-111111111111",
    membership_id="22222222-2222-4222-8222-222222222222",
    role="owner",
)


def _economics(*, start: datetime, end: datetime, today: bool = False) -> UnitEconomicsSnapshot:
    return UnitEconomicsSnapshot(
        business_id=_ACTOR.business_id,
        model_version=RevenueAttributionModel.FIRST_TOUCH_V1,
        occurred_from=start,
        occurred_to=end,
        leads=3 if today else 18,
        qualified_leads=2 if today else 10,
        bookings=1 if today else 7,
        paid_customers=1 if today else 4,
        monetary_outcomes=1 if today else 5,
        attributed_monetary_outcomes=1 if today else 4,
        unattributed_monetary_outcomes=0 if today else 1,
        revenue_by_currency=(MoneyBreakdown(currency="RUB", amount_minor=48_000_00),),
        spend=None,
        cpl_minor=None,
        cost_per_booking_minor=None,
        cac_minor=None,
        roas_basis_points=None,
        limitations=("spend_unavailable",) if today else ("attribution_incomplete", "spend_unavailable"),
        source_breakdown={
            AcquisitionSource.YANDEX_DIRECT: 3,
            AcquisitionSource.REFERRAL: 1,
        },
    )


def _journey(*, start: datetime, end: datetime) -> RevenueJourneySnapshot:
    return RevenueJourneySnapshot(
        business_id=_ACTOR.business_id,
        model_version=RevenueAttributionModel.FIRST_TOUCH_V1,
        occurred_from=start,
        occurred_to=end,
        leads=18,
        qualified_leads=10,
        bookings=7,
        confirmed_bookings=5,
        completed_bookings=4,
        paid_customers=4,
        reactivated_customers=2,
        monetary_outcomes=5,
        attributed_monetary_outcomes=4,
        unattributed_monetary_outcomes=1,
        verified_revenue_by_currency=(MoneyBreakdown(currency="RUB", amount_minor=54_300_00),),
        attributed_revenue_by_currency=(MoneyBreakdown(currency="RUB", amount_minor=48_000_00),),
        unattributed_revenue_by_currency=(MoneyBreakdown(currency="RUB", amount_minor=6_300_00),),
        sources=(
            RevenueJourneySourceResult(
                source=AcquisitionSource.VK,
                leads=11,
                bookings=5,
                completed_bookings=4,
                paid_customers=2,
                reactivated_customers=1,
                revenue_by_currency=(MoneyBreakdown(currency="RUB", amount_minor=34_200_00),),
            ),
            RevenueJourneySourceResult(
                source=AcquisitionSource.YANDEX_DIRECT,
                leads=5,
                bookings=2,
                completed_bookings=0,
                paid_customers=1,
                reactivated_customers=1,
                revenue_by_currency=(MoneyBreakdown(currency="RUB", amount_minor=13_800_00),),
            ),
            RevenueJourneySourceResult(
                source=AcquisitionSource.UNKNOWN,
                leads=2,
                bookings=0,
                completed_bookings=0,
                paid_customers=1,
                reactivated_customers=0,
                revenue_by_currency=(MoneyBreakdown(currency="RUB", amount_minor=6_300_00),),
            ),
        ),
        limitations=("attribution_incomplete", "journey_source_incomplete"),
    )


class GrowthCockpitTests(unittest.TestCase):
    def _build(
        self,
        *,
        period_days: int = 7,
        handoffs=None,
        handoff_count: int | None = None,
        sales=None,
        advertising_loader=None,
    ):
        handoffs = [] if handoffs is None else handoffs
        sales = [] if sales is None else sales
        exact_handoff_count = len(handoffs) if handoff_count is None else int(handoff_count)
        economics_calls: list[tuple[datetime, datetime]] = []

        def fake_economics(*, actor, occurred_from, occurred_to, verified_spend=None):
            self.assertIs(actor, _ACTOR)
            self.assertIsNone(verified_spend)
            economics_calls.append((occurred_from, occurred_to))
            return _economics(
                start=occurred_from,
                end=occurred_to,
                today=len(economics_calls) == 1,
            )

        loader = advertising_loader or (
            lambda **kwargs: SimpleNamespace(
                period_days=kwargs["period_days"],
                date_from="2026-08-12",
                date_to="2026-08-18",
                connected_accounts=1,
                tracked_ads=2,
                impressions=1000,
                clicks=80,
                cost_micros=9_000_000,
                leads=6,
                bookings=3,
                won=2,
            )
        )
        with (
            patch(
                "clientplatform.application.growth_cockpit.get_business_profile",
                return_value=SimpleNamespace(timezone="Europe/Amsterdam"),
            ),
            patch(
                "clientplatform.application.growth_cockpit.get_business_unit_economics",
                side_effect=fake_economics,
            ),
            patch(
                "clientplatform.application.growth_cockpit.get_business_revenue_journey",
                side_effect=lambda *, actor, occurred_from, occurred_to: _journey(
                    start=occurred_from, end=occurred_to
                ),
            ),
            patch(
                "clientplatform.application.growth_cockpit.count_sales_handoff_work",
                side_effect=lambda *, actor: exact_handoff_count,
            ),
            patch(
                "clientplatform.application.growth_cockpit.list_sales_handoff_work",
                side_effect=lambda *, actor, limit: handoffs[:limit],
            ),
            patch(
                "clientplatform.application.growth_cockpit.list_sales_work",
                side_effect=lambda *, actor, limit: sales[:limit],
            ),
        ):
            result = get_growth_cockpit(
                actor=_ACTOR,
                period_days=period_days,
                now=_NOW,
                advertising_loader=loader,
            )
        return result, economics_calls

    def test_today_and_period_use_business_timezone_and_canonical_economics(self) -> None:
        result, calls = self._build(period_days=7)

        self.assertEqual(result.timezone_name, "Europe/Amsterdam")
        self.assertEqual(result.period_days, 7)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0].isoformat(), "2026-08-17T22:00:00+00:00")
        self.assertEqual(calls[0][1].isoformat(), "2026-08-18T22:00:00+00:00")
        self.assertEqual(calls[1][0].isoformat(), "2026-08-11T22:00:00+00:00")
        self.assertEqual(calls[1][1].isoformat(), "2026-08-18T22:00:00+00:00")
        self.assertEqual({item.key: item.value for item in result.today_metrics}["leads"], 3)
        self.assertEqual({item.key: item.value for item in result.period_metrics}["bookings"], 7)

    def test_only_7_or_30_day_periods_are_allowed(self) -> None:
        with self.assertRaisesRegex(ValueError, "7 or 30"):
            self._build(period_days=14)

    def test_handoff_becomes_actionable_next_step(self) -> None:
        result, _ = self._build(
            handoffs=[{"id": "handoff-1", "lead_id": "lead-1", "customer_name": "Анна", "status": "open", "severity": "high"}],
        )

        self.assertEqual(result.needs_reply, 1)
        self.assertEqual(result.next_action.action_key, "sales_handoff")
        self.assertIn("ответ", result.next_action.title.lower())
        self.assertTrue(any("требуют ответа" in item for item in result.attention))

    def test_reply_metric_uses_exact_count_not_detail_limit(self) -> None:
        result, _ = self._build(
            handoffs=[{"id": "handoff-1", "lead_id": "lead-1", "customer_name": "Анна", "status": "open", "severity": "high"}],
            handoff_count=87,
        )

        self.assertEqual(result.needs_reply, 87)
        self.assertTrue(any("87 клиент" in item for item in result.attention))
        self.assertEqual(result.next_action.action_key, "sales_handoff")

    def test_existing_sales_plan_is_reused_instead_of_inventing_second_brain(self) -> None:
        result, _ = self._build(
            sales=[
                {
                    "id": "lead-1",
                    "customer_name": "Мария",
                    "next_plan_id": "plan-1",
                    "next_action_kind": "follow_up",
                    "next_plan_requires_approval": True,
                }
            ]
        )

        self.assertEqual(result.next_action.action_key, "sales_plan:plan-1")
        self.assertEqual(result.next_action.source, "sales_action_plan")
        self.assertIn("Мария", result.next_action.title)
        self.assertEqual(result.actions[0], result.next_action)
        self.assertIn("подтверждение", result.next_action.reason.lower())

    def test_operating_queue_keeps_canonical_source_order_and_caps_at_five(self) -> None:
        result, _ = self._build(
            handoffs=[
                {
                    "id": "handoff-urgent",
                    "lead_id": "lead-h1",
                    "customer_name": "Анна",
                    "status": "open",
                    "severity": "urgent",
                },
                {
                    "id": "handoff-high",
                    "lead_id": "lead-h2",
                    "customer_name": "Борис",
                    "status": "open",
                    "severity": "high",
                },
            ],
            sales=[
                {
                    "id": "lead-s1",
                    "customer_name": "Вера",
                    "next_plan_id": "plan-1",
                    "next_action_kind": "present_offer",
                    "next_plan_requires_approval": True,
                    "due_at": "2026-08-18T13:00:00+00:00",
                },
                {
                    "id": "lead-s2",
                    "customer_name": "Глеб",
                    "next_action": "Позвонить и подтвердить время",
                    "due_at": "2026-08-18T14:00:00+00:00",
                },
                {
                    "id": "lead-s3",
                    "customer_name": "Дина",
                    "next_action": "Уточнить детали",
                },
            ],
        )

        self.assertEqual(len(result.actions), 5)
        self.assertEqual(
            [item.source_id for item in result.actions[:4]],
            ["handoff-urgent", "handoff-high", "plan-1", "lead-s2"],
        )
        self.assertEqual(result.actions[0].action_key, "sales_handoff")
        self.assertEqual(result.actions[2].action_key, "sales_plan:plan-1")
        self.assertEqual(result.actions[3].action_key, "sales_lead:lead-s2")
        self.assertEqual(result.actions[-1].source_id, "lead-s3")
        self.assertFalse(any(item.action_key == "attribution_review" for item in result.actions))

    def test_handoff_suppresses_duplicate_sales_action_for_same_lead(self) -> None:
        result, _ = self._build(
            handoffs=[
                {
                    "id": "handoff-1",
                    "lead_id": "lead-1",
                    "customer_name": "Анна",
                    "status": "open",
                    "severity": "high",
                }
            ],
            sales=[
                {
                    "id": "lead-1",
                    "customer_name": "Анна",
                    "next_action": "Ответить на вопрос",
                }
            ],
        )

        self.assertEqual(sum(item.source_id == "handoff-1" for item in result.actions), 1)
        self.assertFalse(any(item.action_key == "sales_lead:lead-1" for item in result.actions))

    def test_durable_manual_next_action_is_an_operating_queue_item(self) -> None:
        result, _ = self._build(
            sales=[
                {
                    "id": "lead-manual",
                    "customer_name": "Мария",
                    "next_action": "Позвонить после 18:00",
                    "due_at": "2026-08-18T15:00:00+00:00",
                }
            ]
        )

        self.assertEqual(result.next_action.action_key, "sales_lead:lead-manual")
        self.assertEqual(result.next_action.source, "sales_lead")
        self.assertIn("Позвонить после 18:00", result.next_action.reason)
        self.assertIn("Срок:", result.next_action.reason)

    def test_provider_failures_degrade_advertising_without_hiding_business_facts(self) -> None:
        for error_type in (RuntimeError, ValueError, OSError):
            with self.subTest(error_type=error_type.__name__):
                def unavailable(**_kwargs):
                    raise error_type("provider unavailable")

                result, _ = self._build(advertising_loader=unavailable)

                self.assertIsNone(result.advertising)
                self.assertIn("advertising_unavailable", result.limitations)
                self.assertEqual(
                    {item.key: item.value for item in result.period_metrics}["leads"],
                    18,
                )

    def test_summary_hides_provider_money_without_verified_iso_currency(self) -> None:
        result, _ = self._build()
        text = telegram_growth_summary(result)

        self.assertIn("Что происходит с бизнесом", text)
        self.assertIn("Новые лиды: 3", text)
        self.assertIn("Деньги и путь клиента · 7 дней", text)
        self.assertIn("Подтверждённая выручка: 54 300.00 RUB", text)
        self.assertIn("Связано с источником: 48 000.00 RUB", text)
        self.assertIn("Без подтверждённого источника: 6 300.00 RUB", text)
        self.assertIn("Лучший подтверждённый источник: VK — 34 200.00 RUB", text)
        self.assertIn("Вернувшиеся клиенты: 2", text)
        self.assertIn("стоимость скрыта до подтверждения валюты", text)
        self.assertIn("Часть оплат пока нельзя надёжно связать", text)
        self.assertIn("Важные действия", text)
        self.assertIn("Проверить источники оплат", text)
        self.assertIn("Главное действие", text)
        self.assertIn("advertising_currency_unverified", result.limitations)
        self.assertNotIn("CampaignId", text)
        self.assertNotIn("OAuth", text)
        self.assertNotIn("cost_micros", text)
        self.assertNotIn("9.00", text)

    def test_full_payload_keeps_unverified_ad_money_unavailable(self) -> None:
        result, _ = self._build(period_days=30)
        payload = growth_cockpit_payload(result)

        self.assertEqual(payload["period_days"], 30)
        self.assertTrue(payload["today_metrics"])
        for metric in payload["today_metrics"] + payload["period_metrics"]:
            self.assertTrue(metric["source"])
            self.assertTrue(metric["meaning"])
        self.assertEqual(payload["journey"]["source"], "canonical_outcome_revenue_projection")
        self.assertEqual(payload["journey"]["reactivated_customers"], 2)
        self.assertEqual(payload["journey"]["occurred_from"], result.period_from.isoformat())
        self.assertEqual(payload["advertising"]["source"], "verified_yandex_direct_report")
        self.assertIsNone(payload["advertising"]["cost"])
        self.assertNotIn("cost_micros", payload["advertising"])
        self.assertIn("meaning", payload["advertising"])

    @unittest.skipUnless(_AIOGRAM_AVAILABLE, "aiogram is not installed")
    def test_actionable_alerts_link_to_existing_canonical_workflows(self) -> None:
        from handlers.clientplatform_growth import _cockpit_keyboard

        handoff = _cockpit_keyboard(
            business_id=_ACTOR.business_id,
            period_days=7,
            action_key="sales_handoff",
        )
        plan = _cockpit_keyboard(
            business_id=_ACTOR.business_id,
            period_days=30,
            action_key="sales_plan:plan-1",
        )
        lead_id = "33333333-3333-4333-8333-333333333333"
        lead = _cockpit_keyboard(
            business_id=_ACTOR.business_id,
            period_days=7,
            action_key=f"sales_lead:{lead_id}",
        )
        attribution = _cockpit_keyboard(
            business_id=_ACTOR.business_id,
            period_days=7,
            action_key="attribution_review",
        )

        handoff_callbacks = [button.callback_data for row in handoff.inline_keyboard for button in row]
        plan_callbacks = [button.callback_data for row in plan.inline_keyboard for button in row]
        lead_callbacks = [button.callback_data for row in lead.inline_keyboard for button in row]
        attribution_callbacks = [button.callback_data for row in attribution.inline_keyboard for button in row]
        self.assertTrue(any(str(value).startswith("cps:sh:") for value in handoff_callbacks))
        self.assertTrue(any(str(value).startswith("cps:sw:") for value in plan_callbacks))
        self.assertTrue(any(str(value).startswith("cps:swv:") for value in lead_callbacks))
        self.assertTrue(any(str(value).startswith("cpy:a:") for value in attribution_callbacks))
        self.assertTrue(
            any(
                str(value).endswith(":7")
                for value in attribution_callbacks
                if str(value).startswith("cpg:attention:")
            )
        )


@unittest.skipUnless(_AIOGRAM_AVAILABLE, "aiogram is not installed")
class GrowthCockpitHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_today_requires_user_and_handles_empty_business_list(self) -> None:
        from handlers import clientplatform_growth as growth

        missing_user = SimpleNamespace(from_user=None, answer=AsyncMock())
        with self.assertRaisesRegex(ValueError, "Telegram user"):
            await growth.growth_today(missing_user)

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=7001),
            answer=AsyncMock(),
        )
        with patch.object(growth.asyncio, "to_thread", new=AsyncMock(return_value=[])):
            await growth.growth_today(message)

        message.answer.assert_awaited_once_with(
            "Сначала создайте бизнес в ClientPlatform через /start."
        )

    async def test_today_single_business_reuses_canonical_cockpit_sender(self) -> None:
        from handlers import clientplatform_growth as growth

        access = SimpleNamespace(
            business=SimpleNamespace(id=_ACTOR.business_id, name="Бизнес один")
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=7001),
            answer=AsyncMock(),
        )
        sender = AsyncMock()
        with (
            patch.object(growth.asyncio, "to_thread", new=AsyncMock(return_value=[access])),
            patch.object(growth, "_send_cockpit", new=sender),
        ):
            await growth.growth_today(message)

        sender.assert_awaited_once_with(
            message,
            user_id=7001,
            business_id=_ACTOR.business_id,
            period_days=7,
        )
        message.answer.assert_not_awaited()

    async def test_today_multiple_businesses_offers_tenant_scoped_choice(self) -> None:
        from handlers import clientplatform_growth as growth

        other_business_id = "33333333-3333-4333-8333-333333333333"
        accesses = [
            SimpleNamespace(
                business=SimpleNamespace(id=_ACTOR.business_id, name="Первый бизнес")
            ),
            SimpleNamespace(
                business=SimpleNamespace(id=other_business_id, name="Второй бизнес")
            ),
        ]
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=7001),
            answer=AsyncMock(),
        )
        with patch.object(growth.asyncio, "to_thread", new=AsyncMock(return_value=accesses)):
            await growth.growth_today(message)

        text = message.answer.await_args.args[0]
        markup = message.answer.await_args.kwargs["reply_markup"]
        self.assertIn("Для какого бизнеса", text)
        labels = [button.text for row in markup.inline_keyboard for button in row]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertEqual(labels, ["Первый бизнес", "Второй бизнес"])
        self.assertTrue(all(str(value).startswith("cpg:business:") for value in callbacks))

    async def test_business_and_period_callbacks_keep_existing_sender_contract(self) -> None:
        from handlers import clientplatform_growth as growth

        token = growth.uuid_token(_ACTOR.business_id)
        message = SimpleNamespace(answer=AsyncMock())
        sender = AsyncMock()
        chooser = SimpleNamespace(
            data=f"cpg:business:{token}",
            from_user=SimpleNamespace(id=7001),
            answer=AsyncMock(),
        )
        changer = SimpleNamespace(
            data=f"cpg:period:{token}:30",
            from_user=SimpleNamespace(id=7001),
            answer=AsyncMock(),
        )
        with (
            patch.object(growth, "_message", return_value=message),
            patch.object(growth, "_send_cockpit", new=sender),
        ):
            await growth.growth_choose_business(chooser)
            await growth.growth_change_period(changer)

        self.assertEqual(sender.await_count, 2)
        self.assertEqual(sender.await_args_list[0].kwargs["business_id"], _ACTOR.business_id)
        self.assertEqual(sender.await_args_list[0].kwargs["period_days"], 7)
        self.assertEqual(sender.await_args_list[1].kwargs["business_id"], _ACTOR.business_id)
        self.assertEqual(sender.await_args_list[1].kwargs["period_days"], 30)
        chooser.answer.assert_awaited_once()
        changer.answer.assert_awaited_once()

    async def test_period_callback_rejects_malformed_and_unsupported_values(self) -> None:
        from handlers import clientplatform_growth as growth

        for raw_period, expected in (("bad", "period is invalid"), ("14", "7 or 30")):
            with self.subTest(raw_period=raw_period):
                callback = SimpleNamespace(
                    data=f"cpg:period:token:{raw_period}",
                    from_user=SimpleNamespace(id=7001),
                    answer=AsyncMock(),
                )
                with self.assertRaisesRegex(ValueError, expected):
                    await growth.growth_change_period(callback)
                callback.answer.assert_not_awaited()

    async def test_attention_explains_empty_signal_set_and_keeps_navigation(self) -> None:
        from handlers import clientplatform_growth as growth

        token = growth.uuid_token(_ACTOR.business_id)
        snapshot = SimpleNamespace(
            business_id=_ACTOR.business_id,
            period_days=7,
            attention=(),
            actions=(),
            next_action=SimpleNamespace(
                title="Ничего срочного",
                reason="Нет обязательного действия.",
                action_key="none",
            ),
        )
        callback = SimpleNamespace(
            data=f"cpg:attention:{token}:7",
            from_user=SimpleNamespace(id=7001),
            answer=AsyncMock(),
        )
        message = SimpleNamespace(answer=AsyncMock())
        with (
            patch.object(growth, "_message", return_value=message),
            patch.object(growth, "_actor", new=AsyncMock(return_value=_ACTOR)),
            patch.object(growth.asyncio, "to_thread", new=AsyncMock(return_value=snapshot)),
        ):
            await growth.growth_attention(callback)

        callback.answer.assert_awaited_once()
        text = message.answer.await_args.args[0]
        markup = message.answer.await_args.kwargs["reply_markup"]
        self.assertIn("Сейчас нет сигналов", text)
        self.assertIn("Ничего срочного", text)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertFalse(any(str(value).startswith("cpg:attention:") for value in callbacks))
        self.assertTrue(any(str(value).startswith("cp:clients:") for value in callbacks))


if __name__ == "__main__":
    unittest.main()
