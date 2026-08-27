from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import services.db.core as db_core
from services.schema import init_db

import tempfile
import unittest

from clientplatform.application.customer_timeline import (
    CustomerTimeline,
    CustomerTimelineEntry,
    format_customer_timeline_lines,
    get_customer_timeline,
)
from clientplatform.application.customers import create_customer
from clientplatform.application.tenancy import (
    create_business,
    grant_business_member,
    resolve_tenant_context,
)
from clientplatform.domain.customers import CustomerNotFound
from clientplatform.domain.outcomes import (
    BusinessOutcomeEvent,
    OutcomeMoney,
    OutcomeSource,
    OutcomeType,
)
from clientplatform.domain.sales import ContactBasis, SalesLeadStage
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from services.db import get_db


_BASE = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)


def _business(user_id: int, name: str):
    access = create_business(owner_user_id=user_id, name=name)
    actor = resolve_tenant_context(user_id=user_id, business_id=access.business.id)
    customer = create_customer(actor=actor, display_name=f"Клиент {name}")
    with get_db() as conn:
        stamp = _BASE.isoformat(timespec="microseconds")
        conn.execute(
            "UPDATE customers SET created_at=?, updated_at=? WHERE id=? AND business_id=?",
            (stamp, stamp, customer.id, actor.business_id),
        )
    return actor, customer


def _attach_first_touch(*, business_id: str, customer_id: str, at: datetime) -> str:
    identity_id = str(uuid4())
    touch_id = str(uuid4())
    stamp = at.isoformat(timespec="microseconds")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO attribution_identities(
                id, business_id, source, identity_kind, identity_fingerprint,
                source_ref_type, source_ref_id, promotion_campaign_id, created_at
            ) VALUES(?, ?, 'referral', 'test', ?, 'referral', ?, NULL, ?)
            """,
            (
                identity_id,
                business_id,
                (business_id.replace("-", "") * 2)[:64],
                f"referral:{customer_id}",
                stamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO acquisition_touches(
                id, business_id, attribution_identity_id, customer_id, source,
                occurred_at, metadata_json, metadata_version, created_at
            ) VALUES(?, ?, ?, ?, 'referral', ?, '{}', 1, ?)
            """,
            (touch_id, business_id, identity_id, customer_id, stamp, stamp),
        )
        conn.execute(
            """
            INSERT INTO attribution_links(
                id, business_id, touch_id, customer_id, booking_slot_id,
                model_version, created_at
            ) VALUES(?, ?, ?, ?, NULL, 'first_touch_v1', ?)
            """,
            (str(uuid4()), business_id, touch_id, customer_id, stamp),
        )
    return touch_id


def _sales_lead(*, actor, customer_id: str, at: datetime):
    with get_db() as conn:
        sales = SalesRepository(conn)
        lead = sales.create_or_refresh_lead(
            actor=actor,
            opportunity_key=f"timeline:{customer_id}",
            customer_id=customer_id,
            source_kind="referral",
            source_ref="friend",
            contact_basis=ContactBasis.INBOUND,
            now=at.isoformat(),
        )
        return lead


def _stage(*, actor, lead_id: str, stage: SalesLeadStage, at: datetime) -> None:
    with get_db() as conn:
        SalesRepository(conn).set_stage(
            actor=actor,
            lead_id=lead_id,
            stage=stage,
            now=at.isoformat(),
        )


def _outcome(
    *,
    actor,
    customer_id: str,
    outcome_type: OutcomeType,
    at: datetime,
    source_type: str,
    source_id: str,
    amount_minor: int | None = None,
    currency: str | None = None,
) -> str:
    event_id = str(uuid4())
    money = (
        None
        if amount_minor is None
        else OutcomeMoney(amount_minor=amount_minor, currency=str(currency))
    )
    with get_db() as conn:
        OutcomeRepository(conn).append(
            BusinessOutcomeEvent(
                id=event_id,
                business_id=actor.business_id,
                outcome_type=outcome_type,
                occurred_at=at,
                source=OutcomeSource(source_type=source_type, source_id=source_id),
                customer_id=customer_id,
                subject_ref=f"{source_type}:{source_id}",
                money=money,
                idempotency_key=f"timeline:{event_id}",
                metadata={},
                metadata_version=1,
                created_at=at,
            )
        )
    return event_id


class ClientPlatformCustomerTimelineM4002Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_db_path = db_core.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory(prefix="clientplatform-m4002-")
        db_core.DB_PATH = Path(self._tmpdir.name) / "timeline.db"
        init_db()

    def tearDown(self) -> None:
        db_core.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    def test_timeline_projects_acquisition_sales_booking_and_payment_in_order(self) -> None:
        actor, customer = _business(880001, "timeline-happy")
        touch_id = _attach_first_touch(
            business_id=actor.business_id,
            customer_id=customer.id,
            at=_BASE + timedelta(minutes=1),
        )
        lead = _sales_lead(actor=actor, customer_id=customer.id, at=_BASE + timedelta(minutes=2))
        _stage(actor=actor, lead_id=lead.id, stage=SalesLeadStage.QUALIFIED, at=_BASE + timedelta(minutes=3))
        _outcome(
            actor=actor,
            customer_id=customer.id,
            outcome_type=OutcomeType.BOOKING_CREATED,
            at=_BASE + timedelta(minutes=4),
            source_type="booking_slot",
            source_id=str(uuid4()),
        )
        _outcome(
            actor=actor,
            customer_id=customer.id,
            outcome_type=OutcomeType.ORDER_PAID,
            at=_BASE + timedelta(minutes=5),
            source_type="business_payment",
            source_id=str(uuid4()),
            amount_minor=50_000,
            currency="RUB",
        )

        timeline = get_customer_timeline(actor=actor, customer_id=customer.id)
        kinds = [entry.kind for entry in timeline.entries]
        assert kinds[-5:] == [
            "acquisition:first_touch",
            "sales:lead_opened",
            "sales:stage_changed",
            "outcome:booking_created",
            "outcome:order_paid",
        ]
        assert timeline.entries[-5].source_id == touch_id
        assert timeline.entries[-1].amount_minor == 50_000
        assert timeline.entries[-1].currency == "RUB"
        assert [entry.occurred_at for entry in timeline.entries] == sorted(
            entry.occurred_at for entry in timeline.entries
        )


    def test_refund_is_a_distinct_money_fact_and_replay_does_not_duplicate_projection(self) -> None:
        actor, customer = _business(880002, "timeline-refund")
        payment_source = str(uuid4())
        _outcome(
            actor=actor,
            customer_id=customer.id,
            outcome_type=OutcomeType.ORDER_PAID,
            at=_BASE + timedelta(minutes=1),
            source_type="business_payment",
            source_id=payment_source,
            amount_minor=20_000,
            currency="RUB",
        )
        _outcome(
            actor=actor,
            customer_id=customer.id,
            outcome_type=OutcomeType.REFUND_RECORDED,
            at=_BASE + timedelta(minutes=2),
            source_type="business_payment",
            source_id=payment_source,
            amount_minor=20_000,
            currency="RUB",
        )

        first = get_customer_timeline(actor=actor, customer_id=customer.id)
        second = get_customer_timeline(actor=actor, customer_id=customer.id)
        money = [entry for entry in first.entries if entry.amount_minor is not None]
        assert [(entry.kind, entry.amount_minor, entry.currency) for entry in money] == [
            ("outcome:order_paid", 20_000, "RUB"),
            ("outcome:refund_recorded", -20_000, "RUB"),
        ]
        assert first == second
        keys = [(entry.kind, entry.source_type, entry.source_id) for entry in first.entries]
        assert len(keys) == len(set(keys))


    def test_amountless_reversal_resolves_money_and_same_source_facts_stay_distinct(self) -> None:
        actor, customer = _business(880009, "timeline-reversal")
        paid_id = _outcome(
            actor=actor,
            customer_id=customer.id,
            outcome_type=OutcomeType.ORDER_PAID,
            at=_BASE + timedelta(minutes=1),
            source_type="business_payment",
            source_id=str(uuid4()),
            amount_minor=30_000,
            currency="RUB",
        )
        first_correction = _outcome(
            actor=actor,
            customer_id=customer.id,
            outcome_type=OutcomeType.OUTCOME_CORRECTION,
            at=_BASE + timedelta(minutes=2),
            source_type="outcome_event",
            source_id=paid_id,
        )
        second_correction = _outcome(
            actor=actor,
            customer_id=customer.id,
            outcome_type=OutcomeType.OUTCOME_CORRECTION,
            at=_BASE + timedelta(minutes=3),
            source_type="outcome_event",
            source_id=paid_id,
        )
        reversal_id = _outcome(
            actor=actor,
            customer_id=customer.id,
            outcome_type=OutcomeType.OUTCOME_REVERSAL,
            at=_BASE + timedelta(minutes=4),
            source_type="outcome_event",
            source_id=paid_id,
        )

        timeline = get_customer_timeline(actor=actor, customer_id=customer.id)
        outcomes = {
            entry.source_id: entry
            for entry in timeline.entries
            if entry.source_type == "outcome_event"
        }
        self.assertIn(paid_id, outcomes)
        self.assertIn(first_correction, outcomes)
        self.assertIn(second_correction, outcomes)
        self.assertIn(reversal_id, outcomes)
        self.assertEqual(outcomes[reversal_id].amount_minor, -30_000)
        self.assertEqual(outcomes[reversal_id].currency, "RUB")

    def test_partial_history_does_not_invent_missing_sales_or_payment_steps(self) -> None:
        actor, customer = _business(880003, "timeline-partial")
        timeline = get_customer_timeline(actor=actor, customer_id=customer.id)
        assert [entry.kind for entry in timeline.entries] == ["customer:created"]


    def test_cross_tenant_customer_is_rejected(self) -> None:
        actor_a, _customer_a = _business(880004, "timeline-a")
        _actor_b, customer_b = _business(880005, "timeline-b")
        with self.assertRaises(CustomerNotFound):
            get_customer_timeline(actor=actor_a, customer_id=customer_b.id)


    def test_support_role_sees_customer_sales_but_not_attribution_or_money_ledgers(self) -> None:
        owner, customer = _business(880006, "timeline-support")
        _attach_first_touch(
            business_id=owner.business_id,
            customer_id=customer.id,
            at=_BASE + timedelta(minutes=1),
        )
        lead = _sales_lead(actor=owner, customer_id=customer.id, at=_BASE + timedelta(minutes=2))
        _stage(actor=owner, lead_id=lead.id, stage=SalesLeadStage.CONTACTED, at=_BASE + timedelta(minutes=3))
        _outcome(
            actor=owner,
            customer_id=customer.id,
            outcome_type=OutcomeType.ORDER_PAID,
            at=_BASE + timedelta(minutes=4),
            source_type="business_payment",
            source_id=str(uuid4()),
            amount_minor=10_000,
            currency="RUB",
        )
        grant_business_member(actor=owner, user_id=880106, role=PlatformRole.SUPPORT)
        support = resolve_tenant_context(user_id=880106, business_id=owner.business_id)

        timeline = get_customer_timeline(actor=support, customer_id=customer.id)
        kinds = {entry.kind for entry in timeline.entries}
        assert "sales:lead_opened" in kinds
        assert "sales:stage_changed" in kinds
        assert not any(kind.startswith("acquisition:") for kind in kinds)
        assert not any(kind.startswith("outcome:") for kind in kinds)


    def test_non_customer_record_role_is_denied(self) -> None:
        owner, customer = _business(880007, "timeline-marketer")
        grant_business_member(actor=owner, user_id=880107, role=PlatformRole.MARKETER)
        marketer = resolve_tenant_context(user_id=880107, business_id=owner.business_id)
        with self.assertRaises(TenantPermissionDenied):
            get_customer_timeline(actor=marketer, customer_id=customer.id)


    def test_channel_neutral_formatter_keeps_money_explainable_and_bounds_history(self) -> None:
        actor, customer = _business(880008, "timeline-format")
        for index in range(10):
            _outcome(
                actor=actor,
                customer_id=customer.id,
                outcome_type=OutcomeType.ORDER_PAID,
                at=_BASE + timedelta(minutes=index + 1),
                source_type="business_payment",
                source_id=str(uuid4()),
                amount_minor=12_345 + index,
                currency="RUB",
            )
        timeline = get_customer_timeline(actor=actor, customer_id=customer.id, limit=100)
        lines = format_customer_timeline_lines(timeline, max_entries=3)
        assert lines[0] == "• Показаны последние 3 из 11 событий"
        assert len(lines) == 4
        assert "Получена оплата" in lines[-1]
        assert "123,54 RUB" in lines[-1]

    def test_formatter_uses_iso_currency_minor_unit_exponents(self) -> None:
        timeline = CustomerTimeline(
            business_id="business",
            customer_id="customer",
            entries=(
                CustomerTimelineEntry(
                    kind="outcome:order_paid",
                    occurred_at=_BASE,
                    source_type="outcome_event",
                    source_id="jpy-event",
                    title="Получена оплата",
                    amount_minor=500,
                    currency="JPY",
                ),
                CustomerTimelineEntry(
                    kind="outcome:order_paid",
                    occurred_at=_BASE + timedelta(minutes=1),
                    source_type="outcome_event",
                    source_id="kwd-event",
                    title="Получена оплата",
                    amount_minor=1_234,
                    currency="KWD",
                ),
            ),
        )
        lines = format_customer_timeline_lines(timeline)
        self.assertIn("500 JPY", lines[0])
        self.assertIn("1,234 KWD", lines[1])
