from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from clientplatform.domain.outcomes import (
    BusinessOutcomeEvent,
    OutcomeMoney,
    OutcomeSource,
    OutcomeType,
)
from clientplatform.domain.promotions import PromotionChannel, PromotionCreative, stable_creative_id
from clientplatform.domain.revenue_attribution import RevenueAttributionInvariantViolation
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.attribution_repository import AttributionRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.promotion_repository import PromotionRepository
from clientplatform.infrastructure.revenue_attribution_repository import RevenueAttributionRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import (
    clientplatform_activity,
    clientplatform_attribution,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_outcomes,
    clientplatform_promotions,
    clientplatform_revenue_attribution,
    clientplatform_tenancy,
)


_NOW = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)


class ClientPlatformRevenueAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_customers.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_bookings.ensure(self.conn)
        clientplatform_outcomes.ensure(self.conn)
        clientplatform_promotions.ensure(self.conn)
        clientplatform_attribution.ensure(self.conn)
        clientplatform_revenue_attribution.ensure(self.conn)

        self.tenancy = TenancyRepository(self.conn)
        self.activity = ActivityRepository(self.conn)
        self.bookings = BookingRepository(self.conn)
        self.outcomes = OutcomeRepository(self.conn)
        self.promotions = PromotionRepository(self.conn)
        self.attribution = AttributionRepository(self.conn)
        self.revenue = RevenueAttributionRepository(self.conn)

        access = self.tenancy.create_business(owner_user_id=7001, name="Revenue test")
        self.business_id = access.business.id
        self.owner = self.tenancy.resolve_context(user_id=7001, business_id=self.business_id)
        self.activity.upsert_profile(
            actor=self.owner,
            activity_description="Revenue attribution regression",
            timezone_name="Europe/Amsterdam",
            now=_NOW.isoformat(),
        )
        capability = self.activity.enable_capability(
            actor=self.owner,
            connector_key="services",
            now=_NOW.isoformat(),
        )
        offering = self.activity.create_offering(
            actor=self.owner,
            capability_id=capability.id,
            title="Service",
            description="Revenue attribution service",
            now=_NOW.isoformat(),
        )
        self.slot = self.bookings.create_slot(
            actor=self.owner,
            offering_id=offering.id,
            local_start="20.08.2026 12:00",
            duration_minutes=60,
            now=_NOW.isoformat(),
        )
        creative = PromotionCreative(
            creative_id=stable_creative_id("revenue", "attribution"),
            headline="Revenue attribution",
            primary_text="Book now",
            description="Test",
        )
        self.campaign, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=self.slot.slot.id,
            channel=PromotionChannel.TELEGRAM,
            creative=creative,
            now=_NOW.isoformat(),
        )
        self.customer_id = self._connect_customer(77001)
        self.trace = self.attribution.capture_promotion_touch(
            business_id=self.business_id,
            source_token=self.campaign.source_token,
            campaign_id=self.campaign.id,
            channel=self.campaign.channel,
            source_kind="campaign",
            source_key=self.campaign.id,
            customer_id=self.customer_id,
            occurred_at=_NOW,
        )
        claim = self.bookings.book_slot(
            telegram_user_id=77001,
            business_id=self.business_id,
            slot_id=self.slot.slot.id,
            now=(_NOW + timedelta(minutes=1)).isoformat(),
        )
        self.assertEqual(claim.customer_id, self.customer_id)
        self.attribution.link_booking_from_customer(
            business_id=self.business_id,
            customer_id=self.customer_id,
            booking_slot_id=self.slot.slot.id,
            created_at=_NOW + timedelta(minutes=1),
        )
        self._append(
            OutcomeType.LEAD_CREATED,
            event_id="lead-1",
            money=None,
            occurred_at=_NOW + timedelta(minutes=1),
        )
        self._append(
            OutcomeType.LEAD_QUALIFIED,
            event_id="qualified-1",
            money=None,
            occurred_at=_NOW + timedelta(minutes=2),
        )
        self._append(
            OutcomeType.BOOKING_CREATED,
            event_id="booking-1",
            money=None,
            occurred_at=_NOW + timedelta(minutes=3),
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _connect_customer(self, telegram_user_id: int) -> str:
        invite = self.activity.issue_customer_invite(actor=self.owner, now=_NOW.isoformat())
        claim = self.activity.claim_customer_invite(
            token=invite.token,
            telegram_user_id=telegram_user_id,
            username=f"u{telegram_user_id}",
            display_name="Customer",
            now=(_NOW + timedelta(seconds=1)).isoformat(),
        )
        return claim.customer_id

    def _append(
        self,
        outcome_type: OutcomeType,
        *,
        event_id: str,
        money: OutcomeMoney | None,
        occurred_at: datetime,
        customer_id: str | None = None,
        subject_ref: str | None = None,
        source_type: str = "test",
        source_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> BusinessOutcomeEvent:
        return self.outcomes.append(
            BusinessOutcomeEvent(
                id=event_id,
                business_id=self.business_id,
                outcome_type=outcome_type,
                occurred_at=occurred_at,
                source=OutcomeSource(
                    source_type=source_type,
                    source_id=event_id if source_id is None else source_id,
                ),
                customer_id=self.customer_id if customer_id is None else customer_id,
                subject_ref=(
                    f"booking_slot:{self.slot.slot.id}"
                    if subject_ref is None
                    else subject_ref
                ),
                money=money,
                idempotency_key=f"test:{event_id}",
                metadata={} if metadata is None else metadata,
                metadata_version=1,
                created_at=occurred_at,
            )
        )

    def test_touch_booking_payment_and_refund_are_reproducibly_attributed(self) -> None:
        paid = self._append(
            OutcomeType.ORDER_PAID,
            event_id="paid-1",
            money=OutcomeMoney(amount_minor=10_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=4),
        )
        refund = self._append(
            OutcomeType.REFUND_RECORDED,
            event_id="refund-1",
            money=OutcomeMoney(amount_minor=2_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=5),
        )

        first = self.revenue.materialize_outcome(
            business_id=self.business_id,
            outcome_event_id=paid.id,
        )
        replay = self.revenue.materialize_outcome(
            business_id=self.business_id,
            outcome_event_id=paid.id,
        )
        refunded = self.revenue.materialize_outcome(
            business_id=self.business_id,
            outcome_event_id=refund.id,
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(refunded)
        assert first is not None and replay is not None and refunded is not None
        self.assertEqual(replay.id, first.id)
        self.assertEqual(first.touch_id, self.trace.touch.id)
        self.assertEqual(first.promotion_campaign_id, self.campaign.id)
        self.assertEqual(first.amount_minor, 10_000)
        self.assertEqual(refunded.amount_minor, -2_000)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM revenue_attributions").fetchone()[0],
            2,
        )

        snapshot = self.revenue.snapshot(
            business_id=self.business_id,
            occurred_from=_NOW,
            occurred_to=_NOW + timedelta(hours=1),
            verified_spend=OutcomeMoney(amount_minor=4_000, currency="RUB"),
        )
        self.assertTrue(snapshot.attribution_complete)
        self.assertEqual(snapshot.leads, 1)
        self.assertEqual(snapshot.qualified_leads, 1)
        self.assertEqual(snapshot.bookings, 1)
        self.assertEqual(snapshot.paid_customers, 1)
        self.assertEqual(snapshot.attributed_revenue.amount_minor, 8_000)
        self.assertEqual(snapshot.attributed_revenue.currency, "RUB")
        self.assertEqual(snapshot.cpl_minor, 4_000)
        self.assertEqual(snapshot.cost_per_booking_minor, 4_000)
        self.assertEqual(snapshot.cac_minor, 4_000)
        self.assertEqual(snapshot.roas_basis_points, 20_000)

    def test_mixed_currency_is_never_summed_and_roas_is_suppressed(self) -> None:
        self._append(
            OutcomeType.ORDER_PAID,
            event_id="paid-rub",
            money=OutcomeMoney(amount_minor=5_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=4),
        )
        self._append(
            OutcomeType.ORDER_PAID,
            event_id="paid-usd",
            money=OutcomeMoney(amount_minor=2_000, currency="USD"),
            occurred_at=_NOW + timedelta(minutes=5),
        )

        snapshot = self.revenue.snapshot(
            business_id=self.business_id,
            occurred_from=_NOW,
            occurred_to=_NOW + timedelta(hours=1),
            verified_spend=OutcomeMoney(amount_minor=1_000, currency="RUB"),
        )
        self.assertIsNone(snapshot.attributed_revenue)
        self.assertEqual({item.currency for item in snapshot.revenue_by_currency}, {"RUB", "USD"})
        self.assertIsNone(snapshot.roas_basis_points)
        self.assertIn("revenue_mixed_currency", snapshot.limitations)
        self.assertIn("roas_revenue_unavailable", snapshot.limitations)

    def test_unattributed_money_is_explicit_and_never_assigned_by_guess(self) -> None:
        other_customer_id = self._connect_customer(77002)
        paid = self._append(
            OutcomeType.ORDER_PAID,
            event_id="unattributed-paid",
            money=OutcomeMoney(amount_minor=3_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=4),
            customer_id=other_customer_id,
            subject_ref="order:unattributed",
        )
        self.assertIsNone(
            self.revenue.materialize_outcome(
                business_id=self.business_id,
                outcome_event_id=paid.id,
            )
        )
        snapshot = self.revenue.snapshot(
            business_id=self.business_id,
            occurred_from=_NOW,
            occurred_to=_NOW + timedelta(hours=1),
        )
        self.assertFalse(snapshot.attribution_complete)
        self.assertEqual(snapshot.monetary_outcomes, 1)
        self.assertEqual(snapshot.attributed_monetary_outcomes, 0)
        self.assertEqual(snapshot.unattributed_monetary_outcomes, 1)
        self.assertIn("attribution_incomplete", snapshot.limitations)
        self.assertIn("spend_unavailable", snapshot.limitations)

    def test_paid_customers_and_cac_do_not_depend_on_attribution_success(self) -> None:
        other_customer_id = self._connect_customer(77003)
        self._append(
            OutcomeType.ORDER_PAID,
            event_id="unattributed-cac-paid",
            money=OutcomeMoney(amount_minor=6_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=4),
            customer_id=other_customer_id,
            subject_ref="order:unattributed-cac",
        )
        snapshot = self.revenue.snapshot(
            business_id=self.business_id,
            occurred_from=_NOW,
            occurred_to=_NOW + timedelta(hours=1),
            verified_spend=OutcomeMoney(amount_minor=2_500, currency="RUB"),
        )
        self.assertEqual(snapshot.paid_customers, 1)
        self.assertEqual(snapshot.cac_minor, 2_500)
        self.assertFalse(snapshot.attribution_complete)

    def test_cross_tenant_outcome_lookup_fails_closed(self) -> None:
        paid = self._append(
            OutcomeType.ORDER_PAID,
            event_id="tenant-paid",
            money=OutcomeMoney(amount_minor=3_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=4),
        )
        other = self.tenancy.create_business(owner_user_id=9001, name="Other tenant")
        with self.assertRaises(RevenueAttributionInvariantViolation):
            self.revenue.materialize_outcome(
                business_id=other.business.id,
                outcome_event_id=paid.id,
            )

    def test_reversal_reduces_economics_as_a_new_outcome(self) -> None:
        self._append(
            OutcomeType.ORDER_PAID,
            event_id="paid-before-reversal",
            money=OutcomeMoney(amount_minor=7_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=4),
        )
        self._append(
            OutcomeType.OUTCOME_REVERSAL,
            event_id="reversal-1",
            money=OutcomeMoney(amount_minor=7_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=5),
        )
        snapshot = self.revenue.snapshot(
            business_id=self.business_id,
            occurred_from=_NOW,
            occurred_to=_NOW + timedelta(hours=1),
        )
        self.assertEqual(snapshot.attributed_revenue.amount_minor, 0)
        self.assertEqual(snapshot.monetary_outcomes, 2)
        self.assertEqual(snapshot.attributed_monetary_outcomes, 2)

    def test_amountless_canonical_reversal_resolves_referenced_money(self) -> None:
        paid = self._append(
            OutcomeType.ORDER_PAID,
            event_id="paid-for-canonical-reversal",
            money=OutcomeMoney(amount_minor=7_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=4),
        )
        self._append(
            OutcomeType.OUTCOME_REVERSAL,
            event_id="canonical-reversal",
            money=None,
            occurred_at=_NOW + timedelta(minutes=5),
            source_type="outcome_event",
            source_id=paid.id,
            subject_ref=f"outcome:{paid.id}",
        )
        snapshot = self.revenue.snapshot(
            business_id=self.business_id,
            occurred_from=_NOW,
            occurred_to=_NOW + timedelta(hours=1),
        )
        self.assertEqual(snapshot.attributed_revenue.amount_minor, 0)
        self.assertEqual(snapshot.monetary_outcomes, 2)
        self.assertEqual(snapshot.attributed_monetary_outcomes, 2)
        self.assertTrue(snapshot.attribution_complete)

    def test_future_touch_is_never_used_for_earlier_money(self) -> None:
        paid = self._append(
            OutcomeType.ORDER_PAID,
            event_id="paid-before-touch",
            money=OutcomeMoney(amount_minor=1_500, currency="RUB"),
            occurred_at=_NOW - timedelta(seconds=1),
            subject_ref="order:before-touch",
        )
        self.assertIsNone(
            self.revenue.materialize_outcome(
                business_id=self.business_id,
                outcome_event_id=paid.id,
            )
        )
        self.assertIsNone(
            self.revenue.get_for_outcome(
                business_id=self.business_id,
                outcome_event_id=paid.id,
            )
        )

    def test_genuinely_non_monetary_amountless_reversal_is_not_counted(self) -> None:
        self._append(
            OutcomeType.OUTCOME_REVERSAL,
            event_id="amountless-reversal",
            money=None,
            occurred_at=_NOW + timedelta(minutes=4),
        )
        snapshot = self.revenue.snapshot(
            business_id=self.business_id,
            occurred_from=_NOW,
            occurred_to=_NOW + timedelta(hours=1),
        )
        self.assertEqual(snapshot.monetary_outcomes, 0)
        self.assertEqual(snapshot.attributed_monetary_outcomes, 0)
        self.assertEqual(snapshot.unattributed_monetary_outcomes, 0)
        self.assertTrue(snapshot.attribution_complete)
        self.assertNotIn("attribution_incomplete", snapshot.limitations)

    def test_negative_verified_spend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.revenue.snapshot(
                business_id=self.business_id,
                occurred_from=_NOW,
                occurred_to=_NOW + timedelta(hours=1),
                verified_spend=OutcomeMoney(amount_minor=-1, currency="RUB"),
            )

    def test_financial_evidence_survives_source_privacy_detach(self) -> None:
        paid = self._append(
            OutcomeType.ORDER_PAID,
            event_id="privacy-paid",
            money=OutcomeMoney(amount_minor=5_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=4),
        )
        record = self.revenue.materialize_outcome(
            business_id=self.business_id,
            outcome_event_id=paid.id,
        )
        self.assertIsNotNone(record)
        assert record is not None

        self.conn.execute(
            "DELETE FROM promotion_campaigns WHERE id=? AND business_id=?",
            (self.campaign.id, self.business_id),
        )
        retained = self.revenue.get_for_outcome(
            business_id=self.business_id,
            outcome_event_id=paid.id,
        )
        self.assertIsNotNone(retained)
        assert retained is not None
        self.assertIsNone(retained.touch_id)
        self.assertIsNone(retained.attribution_identity_id)
        self.assertIsNone(retained.promotion_campaign_id)
        self.assertEqual(retained.amount_minor, 5_000)
        self.assertEqual(retained.currency, "RUB")
        self.assertEqual(retained.source_ref_id, record.source_ref_id)

        replay = self.revenue.materialize_outcome(
            business_id=self.business_id,
            outcome_event_id=paid.id,
        )
        self.assertEqual(replay, retained)

        journey = self.revenue.journey_snapshot(
            business_id=self.business_id,
            occurred_from=_NOW,
            occurred_to=_NOW + timedelta(hours=1),
        )
        self.assertTrue(journey.attribution_complete)
        self.assertEqual(journey.attributed_monetary_outcomes, 1)
        self.assertEqual(journey.unattributed_monetary_outcomes, 0)
        self.assertEqual(journey.attributed_revenue_by_currency[0].amount_minor, 5_000)
        telegram = next(item for item in journey.sources if item.source.value == "telegram")
        self.assertEqual(telegram.paid_customers, 1)
        self.assertEqual(telegram.revenue_by_currency[0].amount_minor, 5_000)


    def test_refund_never_uses_a_later_touch_when_original_payment_was_unattributed(self) -> None:
        customer_id = self._connect_customer(77008)
        paid = self._append(
            OutcomeType.ORDER_PAID,
            event_id="unattributed-before-later-touch",
            money=OutcomeMoney(amount_minor=5_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=4),
            customer_id=customer_id,
            subject_ref="order:before-later-touch",
        )
        self.assertIsNone(
            self.revenue.materialize_outcome(
                business_id=self.business_id,
                outcome_event_id=paid.id,
            )
        )
        later_trace = self.attribution.capture_promotion_touch(
            business_id=self.business_id,
            source_token=self.campaign.source_token,
            campaign_id=self.campaign.id,
            channel=self.campaign.channel,
            source_kind="campaign",
            source_key=self.campaign.id,
            customer_id=customer_id,
            occurred_at=_NOW + timedelta(minutes=5),
        )
        self.assertEqual(later_trace.identity.source.value, "telegram")
        refund = self._append(
            OutcomeType.REFUND_RECORDED,
            event_id="refund-after-later-touch",
            money=OutcomeMoney(amount_minor=5_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=6),
            customer_id=customer_id,
            source_type="business_payment",
            source_id="payment-unattributed",
            subject_ref="business_payment:payment-unattributed",
            metadata={"payment_outcome_event_id": paid.id},
        )
        # Simulate a row created by pre-fix main: the refund was independently
        # attributed to the customer's later acquisition touch. Upgrade replay
        # must repair it rather than trusting that stale durable decision.
        self.conn.execute(
            """
            INSERT INTO revenue_attributions(
                id, business_id, outcome_event_id, outcome_type, customer_id,
                touch_id, attribution_identity_id, source, source_ref_type,
                source_ref_id, promotion_campaign_id, model_version,
                amount_minor, currency, occurred_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-refund-after-later-touch",
                self.business_id,
                refund.id,
                refund.outcome_type.value,
                customer_id,
                later_trace.touch.id,
                later_trace.identity.id,
                later_trace.identity.source.value,
                later_trace.identity.source_ref_type,
                later_trace.identity.source_ref_id,
                later_trace.identity.promotion_campaign_id,
                "first_touch_v1",
                -5_000,
                "RUB",
                refund.occurred_at.isoformat(timespec="microseconds"),
                refund.created_at.isoformat(timespec="microseconds"),
            ),
        )
        legacy = self.revenue.get_for_outcome(
            business_id=self.business_id,
            outcome_event_id=refund.id,
        )
        self.assertIsNotNone(legacy)
        assert legacy is not None
        self.assertEqual(legacy.source.value, "telegram")

        repaired = self.revenue.materialize_outcome(
            business_id=self.business_id,
            outcome_event_id=refund.id,
        )
        self.assertIsNotNone(repaired)
        assert repaired is not None
        self.assertEqual(repaired.source.value, "unknown")
        self.assertIsNone(repaired.touch_id)
        self.assertIsNone(repaired.attribution_identity_id)
        self.assertIsNone(repaired.promotion_campaign_id)
        self.assertEqual(repaired.source_ref_type, "outcome_event")
        self.assertEqual(repaired.source_ref_id, paid.id)
        journey = self.revenue.journey_snapshot(
            business_id=self.business_id,
            occurred_from=_NOW + timedelta(minutes=4),
            occurred_to=_NOW + timedelta(minutes=7),
        )
        self.assertEqual(journey.monetary_outcomes, 2)
        self.assertEqual(journey.attributed_monetary_outcomes, 0)
        self.assertEqual(journey.unattributed_monetary_outcomes, 2)
        self.assertTrue(all(item.source.value != "telegram" for item in journey.sources))

    def test_refund_and_reversal_inherit_durable_source_after_privacy_detach(self) -> None:
        paid = self._append(
            OutcomeType.ORDER_PAID,
            event_id="privacy-paid-before-corrections",
            money=OutcomeMoney(amount_minor=5_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=4),
        )
        paid_record = self.revenue.materialize_outcome(
            business_id=self.business_id,
            outcome_event_id=paid.id,
        )
        self.assertIsNotNone(paid_record)
        assert paid_record is not None
        self.conn.execute(
            "DELETE FROM promotion_campaigns WHERE id=? AND business_id=?",
            (self.campaign.id, self.business_id),
        )
        retained = self.revenue.get_for_outcome(
            business_id=self.business_id,
            outcome_event_id=paid.id,
        )
        self.assertIsNotNone(retained)
        assert retained is not None
        self.assertEqual(retained.source.value, "telegram")
        self.assertIsNone(retained.touch_id)

        refund = self._append(
            OutcomeType.REFUND_RECORDED,
            event_id="privacy-refund-after-detach",
            money=OutcomeMoney(amount_minor=2_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=5),
            source_type="business_payment",
            source_id="payment-privacy-refund",
            subject_ref="business_payment:payment-privacy-refund",
            metadata={"payment_outcome_event_id": paid.id},
        )
        reversal = self._append(
            OutcomeType.OUTCOME_REVERSAL,
            event_id="privacy-reversal-after-detach",
            money=OutcomeMoney(amount_minor=3_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=6),
            source_type="outcome_event",
            source_id=paid.id,
            subject_ref=f"outcome:{paid.id}",
        )
        refunded = self.revenue.materialize_outcome(
            business_id=self.business_id,
            outcome_event_id=refund.id,
        )
        reversed_record = self.revenue.materialize_outcome(
            business_id=self.business_id,
            outcome_event_id=reversal.id,
        )
        self.assertIsNotNone(refunded)
        self.assertIsNotNone(reversed_record)
        assert refunded is not None and reversed_record is not None
        for correction in (refunded, reversed_record):
            self.assertEqual(correction.source, retained.source)
            self.assertEqual(correction.source_ref_type, retained.source_ref_type)
            self.assertEqual(correction.source_ref_id, retained.source_ref_id)
            self.assertIsNone(correction.touch_id)
        self.assertEqual(refunded.amount_minor, -2_000)
        self.assertEqual(reversed_record.amount_minor, -3_000)

        journey = self.revenue.journey_snapshot(
            business_id=self.business_id,
            occurred_from=_NOW + timedelta(minutes=4),
            occurred_to=_NOW + timedelta(minutes=7),
        )
        self.assertTrue(journey.attribution_complete)
        self.assertEqual(journey.monetary_outcomes, 3)
        self.assertEqual(journey.attributed_monetary_outcomes, 3)
        self.assertEqual(journey.unattributed_monetary_outcomes, 0)
        self.assertEqual(
            [(item.currency, item.amount_minor) for item in journey.verified_revenue_by_currency],
            [("RUB", 0)],
        )
        self.assertEqual(
            [(item.currency, item.amount_minor) for item in journey.attributed_revenue_by_currency],
            [("RUB", 0)],
        )
        self.assertEqual(journey.unattributed_revenue_by_currency, ())
        telegram = next(item for item in journey.sources if item.source.value == "telegram")
        self.assertEqual(telegram.revenue_by_currency[0].amount_minor, 0)

    def test_money_cockpit_journey_keeps_verified_money_separate_from_source_attribution(self) -> None:
        self._append(
            OutcomeType.BOOKING_CONFIRMED,
            event_id="booking-confirmed-1",
            money=None,
            occurred_at=_NOW + timedelta(minutes=4),
        )
        self._append(
            OutcomeType.BOOKING_COMPLETED,
            event_id="booking-completed-1",
            money=None,
            occurred_at=_NOW + timedelta(minutes=5),
        )
        self._append(
            OutcomeType.ORDER_PAID,
            event_id="journey-paid",
            money=OutcomeMoney(amount_minor=10_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=6),
        )
        self._append(
            OutcomeType.REFUND_RECORDED,
            event_id="journey-refund",
            money=OutcomeMoney(amount_minor=2_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=7),
        )
        self._append(
            OutcomeType.CUSTOMER_REACTIVATED,
            event_id="journey-reactivated",
            money=OutcomeMoney(amount_minor=10_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=8),
            subject_ref="sales_lead:reactivation-1",
            source_type="outcome_event",
            source_id="journey-paid",
        )
        unattributed_customer = self._connect_customer(77004)
        self._append(
            OutcomeType.ORDER_PAID,
            event_id="journey-unattributed-paid",
            money=OutcomeMoney(amount_minor=3_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=9),
            customer_id=unattributed_customer,
            subject_ref="order:unattributed",
        )

        journey = self.revenue.journey_snapshot(
            business_id=self.business_id,
            occurred_from=_NOW,
            occurred_to=_NOW + timedelta(hours=1),
        )

        self.assertEqual(journey.leads, 1)
        self.assertEqual(journey.qualified_leads, 1)
        self.assertEqual(journey.bookings, 1)
        self.assertEqual(journey.confirmed_bookings, 1)
        self.assertEqual(journey.completed_bookings, 1)
        self.assertEqual(journey.paid_customers, 2)
        self.assertEqual(journey.reactivated_customers, 1)
        self.assertEqual(journey.monetary_outcomes, 3)
        self.assertEqual(journey.attributed_monetary_outcomes, 2)
        self.assertEqual(journey.unattributed_monetary_outcomes, 1)
        self.assertEqual(
            [(item.currency, item.amount_minor) for item in journey.verified_revenue_by_currency],
            [("RUB", 11_000)],
        )
        self.assertEqual(
            [(item.currency, item.amount_minor) for item in journey.attributed_revenue_by_currency],
            [("RUB", 8_000)],
        )
        self.assertEqual(
            [(item.currency, item.amount_minor) for item in journey.unattributed_revenue_by_currency],
            [("RUB", 3_000)],
        )
        self.assertFalse(journey.attribution_complete)
        self.assertIn("attribution_incomplete", journey.limitations)
        self.assertIn("booking_completion_unavailable", journey.limitations)

        telegram = next(item for item in journey.sources if item.source.value == "telegram")
        unknown = next(item for item in journey.sources if item.source.value == "unknown")
        self.assertEqual(telegram.leads, 1)
        self.assertEqual(telegram.bookings, 1)
        self.assertEqual(telegram.completed_bookings, 1)
        self.assertEqual(telegram.paid_customers, 1)
        self.assertEqual(telegram.reactivated_customers, 1)
        self.assertEqual(telegram.revenue_by_currency[0].amount_minor, 8_000)
        self.assertEqual(unknown.paid_customers, 1)
        self.assertEqual(unknown.revenue_by_currency[0].amount_minor, 3_000)
        self.assertEqual(journey.sources[0].source.value, "telegram")



    def test_money_cockpit_treats_durable_unknown_source_as_unattributed(self) -> None:
        slot = self.bookings.create_slot(
            actor=self.owner,
            offering_id=self.campaign.offering_id,
            local_start="21.08.2026 12:00",
            duration_minutes=60,
            now=_NOW.isoformat(),
        )
        creative = PromotionCreative(
            creative_id=stable_creative_id("revenue", "unknown-max"),
            headline="Unknown source regression",
            primary_text="Book now",
            description="Test",
        )
        campaign, _ = self.promotions.create_or_refresh_campaign(
            actor=self.owner,
            slot_id=slot.slot.id,
            channel=PromotionChannel.MAX,
            creative=creative,
            now=_NOW.isoformat(),
        )
        customer_id = self._connect_customer(77007)
        trace = self.attribution.capture_promotion_touch(
            business_id=self.business_id,
            source_token=campaign.source_token,
            campaign_id=campaign.id,
            channel=campaign.channel,
            source_kind="campaign",
            source_key=campaign.id,
            customer_id=customer_id,
            occurred_at=_NOW + timedelta(minutes=4),
        )
        self.assertEqual(trace.identity.source.value, "unknown")
        paid = self._append(
            OutcomeType.ORDER_PAID,
            event_id="journey-unknown-source-paid",
            money=OutcomeMoney(amount_minor=5_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=5),
            customer_id=customer_id,
            subject_ref="order:unknown-source",
        )
        record = self.revenue.materialize_outcome(
            business_id=self.business_id,
            outcome_event_id=paid.id,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.source.value, "unknown")

        journey = self.revenue.journey_snapshot(
            business_id=self.business_id,
            occurred_from=_NOW + timedelta(minutes=4),
            occurred_to=_NOW + timedelta(minutes=6),
        )
        self.assertEqual(journey.monetary_outcomes, 1)
        self.assertEqual(journey.attributed_monetary_outcomes, 0)
        self.assertEqual(journey.unattributed_monetary_outcomes, 1)
        self.assertEqual(journey.attributed_revenue_by_currency, ())
        self.assertEqual(
            [(item.currency, item.amount_minor) for item in journey.unattributed_revenue_by_currency],
            [("RUB", 5_000)],
        )
        self.assertIn("attribution_incomplete", journey.limitations)
        unknown = next(item for item in journey.sources if item.source.value == "unknown")
        self.assertEqual(unknown.paid_customers, 1)
        self.assertEqual(unknown.revenue_by_currency[0].amount_minor, 5_000)

    def test_money_cockpit_never_combines_mixed_currencies_for_source_ranking(self) -> None:
        self._append(
            OutcomeType.ORDER_PAID,
            event_id="journey-rub",
            money=OutcomeMoney(amount_minor=7_000, currency="RUB"),
            occurred_at=_NOW + timedelta(minutes=4),
        )
        other_customer = self._connect_customer(77005)
        self._append(
            OutcomeType.ORDER_PAID,
            event_id="journey-usd-unattributed",
            money=OutcomeMoney(amount_minor=50_00, currency="USD"),
            occurred_at=_NOW + timedelta(minutes=5),
            customer_id=other_customer,
            subject_ref="order:usd-unattributed",
        )

        journey = self.revenue.journey_snapshot(
            business_id=self.business_id,
            occurred_from=_NOW,
            occurred_to=_NOW + timedelta(hours=1),
        )

        self.assertEqual(
            [(item.currency, item.amount_minor) for item in journey.verified_revenue_by_currency],
            [("RUB", 7_000), ("USD", 5_000)],
        )
        self.assertIn("verified_revenue_mixed_currency", journey.limitations)
        self.assertEqual(journey.sources[0].source.value, "telegram")
        self.assertEqual(journey.sources[-1].source.value, "unknown")

    def test_money_cockpit_marks_stage_source_unknown_without_guessing(self) -> None:
        other_customer = self._connect_customer(77006)
        self._append(
            OutcomeType.LEAD_CREATED,
            event_id="journey-unknown-lead",
            money=None,
            occurred_at=_NOW + timedelta(minutes=4),
            customer_id=other_customer,
            subject_ref="lead:unknown",
        )

        journey = self.revenue.journey_snapshot(
            business_id=self.business_id,
            occurred_from=_NOW,
            occurred_to=_NOW + timedelta(hours=1),
        )

        unknown = next(item for item in journey.sources if item.source.value == "unknown")
        self.assertEqual(unknown.leads, 1)
        self.assertIn("journey_source_incomplete", journey.limitations)



if __name__ == "__main__":
    unittest.main()
