from __future__ import annotations

import threading
from unittest import TestCase, mock
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from clientplatform.application import admin_ops
from clientplatform.application.activity import (
    create_business_offering,
    enable_business_capability,
    save_business_profile,
)
from clientplatform.application.admin_ops import (
    PaymentEvidenceInvariantViolation,
    PaymentIdempotencyConflict,
    PaymentStateConflict,
    list_payments,
    record_payment,
    refund_payment,
    set_offering_price,
)
from clientplatform.application.customers import create_customer
from clientplatform.application.tenancy import (
    create_business,
    resolve_tenant_context,
)
from clientplatform.privacy_manifest import (
    TENANT_POLICIES,
    validate_clientplatform_privacy_manifest,
)
from services.db import get_db, get_db_ro
from services.migrations import clientplatform_business_payment_outcomes_v1


_NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
_ASSERTIONS = TestCase()


def _business(user_id: int, name: str):
    access = create_business(owner_user_id=user_id, name=name)
    actor = resolve_tenant_context(
        user_id=user_id,
        business_id=access.business.id,
    )
    save_business_profile(
        actor=actor,
        activity_description=f"Payment evidence for {name}",
        timezone_name="Europe/Moscow",
    )
    capability = enable_business_capability(
        actor=actor,
        connector_key="services",
    )
    offering = create_business_offering(
        actor=actor,
        capability_id=capability.id,
        title="Consultation",
        description="Canonical paid consultation",
    )
    customer = create_customer(actor=actor, display_name=f"Customer {name}")
    return actor, offering, customer


def _attach_first_touch(*, business_id: str, customer_id: str) -> None:
    occurred_at = (_NOW - timedelta(hours=1)).isoformat(timespec="microseconds")
    identity_id = str(uuid4())
    touch_id = str(uuid4())
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
                ("a" * 32 + business_id.replace("-", ""))[:64].ljust(64, "b"),
                f"referral:{customer_id}",
                occurred_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO acquisition_touches(
                id, business_id, attribution_identity_id, customer_id, source,
                occurred_at, metadata_json, metadata_version, created_at
            ) VALUES(?, ?, ?, ?, 'referral', ?, '{}', 1, ?)
            """,
            (
                touch_id,
                business_id,
                identity_id,
                customer_id,
                occurred_at,
                occurred_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO attribution_links(
                id, business_id, touch_id, customer_id, booking_slot_id,
                model_version, created_at
            ) VALUES(?, ?, ?, ?, NULL, 'first_touch_v1', ?)
            """,
            (
                str(uuid4()),
                business_id,
                touch_id,
                customer_id,
                occurred_at,
            ),
        )


def _business_payment_counts(business_id: str) -> tuple[int, int, int, int]:
    with get_db_ro() as conn:
        payments = conn.execute(
            "SELECT COUNT(*) AS c FROM business_payments WHERE business_id=?",
            (business_id,),
        ).fetchone()
        evidence = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM business_payment_outcome_evidence
            WHERE business_id=?
            """,
            (business_id,),
        ).fetchone()
        outcomes = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM business_outcome_events
            WHERE business_id=? AND source_type='business_payment'
            """,
            (business_id,),
        ).fetchone()
        revenue = conn.execute(
            "SELECT COUNT(*) AS c FROM revenue_attributions WHERE business_id=?",
            (business_id,),
        ).fetchone()
    return tuple(int(row["c"]) for row in (payments, evidence, outcomes, revenue))


def test_provider_confirmation_exact_replay_and_revenue_bridge() -> None:
    actor, offering, customer = _business(840001, "provider-confirmation")
    set_offering_price(
        actor=actor,
        offering_id=offering.id,
        amount_minor=50_000,
        currency="RUB",
    )
    _attach_first_touch(
        business_id=actor.business_id,
        customer_id=customer.id,
    )

    request = {
        "actor": actor,
        "customer_id": customer.id,
        "offering_id": offering.id,
        "amount_minor": 50_000,
        "currency": "RUB",
        "provider": "customer_acquirer",
        "external_reference": "payment-event-840001",
        "idempotency_key": "provider-payment-event-840001",
        "note": "Paid consultation",
        "now": _NOW,
    }
    first = record_payment(**request)
    replay = record_payment(**request)

    assert replay == first
    assert first.status == "paid"
    assert first.customer_id == customer.id
    assert first.offering_id == offering.id
    assert first.outcome_event_id is not None
    assert first.revenue_attribution_id is not None
    assert _business_payment_counts(actor.business_id) == (1, 1, 1, 1)

    with get_db_ro() as conn:
        outcome = conn.execute(
            """
            SELECT outcome_type, amount_minor, currency, source_id
            FROM business_outcome_events
            WHERE business_id=? AND id=?
            """,
            (actor.business_id, first.outcome_event_id),
        ).fetchone()
        revenue = conn.execute(
            """
            SELECT amount_minor, currency
            FROM revenue_attributions
            WHERE business_id=? AND outcome_event_id=?
            """,
            (actor.business_id, first.outcome_event_id),
        ).fetchone()
    assert dict(outcome) == {
        "outcome_type": "order_paid",
        "amount_minor": 50_000,
        "currency": "RUB",
        "source_id": first.id,
    }
    assert dict(revenue) == {"amount_minor": 50_000, "currency": "RUB"}

    with _ASSERTIONS.assertRaises(PaymentIdempotencyConflict):
        record_payment(**{**request, "amount_minor": 50_001})
    with _ASSERTIONS.assertRaises(PaymentIdempotencyConflict):
        record_payment(
            **{
                **request,
                "idempotency_key": "provider-payment-event-840001-again",
            }
        )
    assert _business_payment_counts(actor.business_id) == (1, 1, 1, 1)


def test_provider_evidence_mismatch_fails_closed() -> None:
    actor, _offering, customer = _business(840009, "provider-invariant")
    request = {
        "actor": actor,
        "customer_id": customer.id,
        "amount_minor": 12_000,
        "currency": "RUB",
        "provider": "customer_acquirer",
        "external_reference": "payment-event-840009",
        "idempotency_key": "provider-payment-event-840009",
        "now": _NOW,
    }
    payment = record_payment(**request)

    with get_db() as conn:
        conn.execute(
            """
            UPDATE business_payments
            SET provider='tampered_provider'
            WHERE business_id=? AND id=?
            """,
            (actor.business_id, payment.id),
        )

    with _ASSERTIONS.assertRaisesRegex(
        PaymentEvidenceInvariantViolation,
        "provider evidence disagrees",
    ):
        record_payment(**request)
    with _ASSERTIONS.assertRaisesRegex(
        PaymentEvidenceInvariantViolation,
        "provider evidence disagrees",
    ):
        refund_payment(
            actor=actor,
            payment_id=payment.id,
            idempotency_key="provider-invariant-refund",
        )
    assert _business_payment_counts(actor.business_id)[:3] == (1, 1, 1)


def test_refund_is_separate_money_fact_and_double_refund_fails_closed() -> None:
    actor, _offering, customer = _business(840002, "refund")
    _attach_first_touch(
        business_id=actor.business_id,
        customer_id=customer.id,
    )
    payment = record_payment(
        actor=actor,
        customer_id=customer.id,
        amount_minor=25_000,
        currency="RUB",
        idempotency_key="owner-payment-840002",
        now=_NOW,
    )
    request = {
        "actor": actor,
        "payment_id": payment.id,
        "idempotency_key": "owner-refund-840002",
        "reason": "Customer cancellation",
        "now": _NOW + timedelta(minutes=5),
    }

    refunded = refund_payment(**request)
    replay = refund_payment(**request)

    assert replay == refunded
    assert refunded.status == "refunded"
    assert refunded.refunded_at is not None
    assert refunded.refund_outcome_event_id is not None
    assert refunded.refund_revenue_attribution_id is not None
    assert _business_payment_counts(actor.business_id) == (1, 2, 2, 2)

    with get_db_ro() as conn:
        amounts = conn.execute(
            """
            SELECT outcome_type, amount_minor
            FROM revenue_attributions
            WHERE business_id=?
            ORDER BY amount_minor DESC
            """,
            (actor.business_id,),
        ).fetchall()
    assert [(row["outcome_type"], row["amount_minor"]) for row in amounts] == [
        ("order_paid", 25_000),
        ("refund_recorded", -25_000),
    ]

    with _ASSERTIONS.assertRaises(PaymentIdempotencyConflict):
        refund_payment(**{**request, "reason": "Changed reason"})
    with _ASSERTIONS.assertRaises(PaymentStateConflict):
        refund_payment(
            **{
                **request,
                "idempotency_key": "second-refund-840002",
            }
        )
    assert _business_payment_counts(actor.business_id) == (1, 2, 2, 2)


def test_payment_outcome_and_audit_roll_back_together() -> None:
    actor, _offering, customer = _business(840003, "rollback")

    def fail_materialization(*_args, **_kwargs):
        raise RuntimeError("forced revenue materialization failure")

    with mock.patch.object(
        admin_ops.RevenueAttributionRepository,
        "materialize_outcome",
        fail_materialization,
    ):
        with _ASSERTIONS.assertRaisesRegex(RuntimeError, "forced revenue"):
            record_payment(
                actor=actor,
                customer_id=customer.id,
                amount_minor=11_000,
                currency="RUB",
                idempotency_key="rollback-payment-840003",
                now=_NOW,
            )

    assert _business_payment_counts(actor.business_id) == (0, 0, 0, 0)
    with get_db_ro() as conn:
        audit_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM clientplatform_admin_audit_events
            WHERE business_id=? AND action='payment_recorded'
            """,
            (actor.business_id,),
        ).fetchone()["c"]
    assert audit_count == 0

    recovered = record_payment(
        actor=actor,
        customer_id=customer.id,
        amount_minor=11_000,
        currency="RUB",
        idempotency_key="rollback-payment-840003",
        now=_NOW,
    )
    assert recovered.outcome_event_id is not None
    assert _business_payment_counts(actor.business_id) == (1, 1, 1, 0)


def test_customer_offering_payment_and_idempotency_are_tenant_scoped() -> None:
    first, first_offering, first_customer = _business(840004, "first-tenant")
    second, _second_offering, second_customer = _business(840005, "second-tenant")

    with _ASSERTIONS.assertRaisesRegex(ValueError, "active customer"):
        record_payment(
            actor=second,
            customer_id=first_customer.id,
            amount_minor=9_000,
            currency="RUB",
            idempotency_key="shared-business-key",
        )
    with _ASSERTIONS.assertRaisesRegex(ValueError, "active offering"):
        record_payment(
            actor=second,
            customer_id=second_customer.id,
            offering_id=first_offering.id,
            amount_minor=9_000,
            currency="RUB",
            idempotency_key="shared-business-key",
        )

    first_payment = record_payment(
        actor=first,
        customer_id=first_customer.id,
        amount_minor=9_000,
        currency="RUB",
        idempotency_key="shared-business-key",
    )
    second_payment = record_payment(
        actor=second,
        customer_id=second_customer.id,
        amount_minor=9_000,
        currency="RUB",
        idempotency_key="shared-business-key",
    )
    assert first_payment.business_id != second_payment.business_id
    assert len(list_payments(actor=first)) == 1
    assert len(list_payments(actor=second)) == 1

    with _ASSERTIONS.assertRaisesRegex(ValueError, "not found in this business"):
        refund_payment(
            actor=second,
            payment_id=first_payment.id,
            idempotency_key="cross-tenant-refund",
        )
    assert _business_payment_counts(first.business_id)[:3] == (1, 1, 1)
    assert _business_payment_counts(second.business_id)[:3] == (1, 1, 1)


def test_offering_currency_and_unknown_currency_fail_closed_without_orphans() -> None:
    actor, offering, customer = _business(840006, "currency")
    set_offering_price(
        actor=actor,
        offering_id=offering.id,
        amount_minor=18_000,
        currency="RUB",
    )

    with _ASSERTIONS.assertRaisesRegex(PaymentStateConflict, "offering price"):
        record_payment(
            actor=actor,
            customer_id=customer.id,
            offering_id=offering.id,
            amount_minor=18_000,
            currency="USD",
            idempotency_key="mixed-currency-payment",
        )
    with _ASSERTIONS.assertRaisesRegex(ValueError, "known ISO 4217"):
        record_payment(
            actor=actor,
            customer_id=customer.id,
            amount_minor=18_000,
            currency="ZZZ",
            idempotency_key="unknown-currency-payment",
        )
    with _ASSERTIONS.assertRaisesRegex(ValueError, "known ISO 4217"):
        record_payment(
            actor=actor,
            customer_id=customer.id,
            amount_minor=18_000,
            currency="XXX",
            idempotency_key="no-currency-payment",
        )
    with _ASSERTIONS.assertRaisesRegex(ValueError, "three Latin"):
        record_payment(
            actor=actor,
            customer_id=customer.id,
            amount_minor=18_000,
            currency="",
            idempotency_key="missing-currency-payment",
        )
    with _ASSERTIONS.assertRaisesRegex(ValueError, "amount_minor"):
        record_payment(
            actor=actor,
            customer_id=customer.id,
            amount_minor=18_000.5,  # type: ignore[arg-type]
            currency="RUB",
            idempotency_key="fractional-minor-units",
        )
    with _ASSERTIONS.assertRaisesRegex(ValueError, "external_reference"):
        record_payment(
            actor=actor,
            customer_id=customer.id,
            amount_minor=18_000,
            currency="RUB",
            provider="customer_acquirer",
            idempotency_key="provider-without-reference",
        )
    assert _business_payment_counts(actor.business_id) == (0, 0, 0, 0)


def test_concurrent_owner_or_provider_retries_create_exactly_one_fact() -> None:
    actor, _offering, customer = _business(840007, "concurrency")
    gate = threading.Barrier(2)

    def confirm_same_callback(_position: int):
        gate.wait(timeout=10)
        return record_payment(
            actor=actor,
            customer_id=customer.id,
            amount_minor=33_000,
            currency="RUB",
            provider="customer_acquirer",
            external_reference="concurrent-provider-event",
            idempotency_key="concurrent-provider-key",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        same_results = list(pool.map(confirm_same_callback, range(2)))
    assert same_results[0].id == same_results[1].id
    assert same_results[0].outcome_event_id == same_results[1].outcome_event_id
    assert _business_payment_counts(actor.business_id)[:3] == (1, 1, 1)

    second_gate = threading.Barrier(2)

    def confirm_conflicting_callback(position: int):
        second_gate.wait(timeout=10)
        try:
            return record_payment(
                actor=actor,
                customer_id=customer.id,
                amount_minor=44_000,
                currency="RUB",
                provider="customer_acquirer",
                external_reference="same-external-different-key",
                idempotency_key=f"provider-race-{position}",
            )
        except PaymentIdempotencyConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        conflicting_results = list(pool.map(confirm_conflicting_callback, range(2)))
    assert sum(result == "conflict" for result in conflicting_results) == 1
    assert _business_payment_counts(actor.business_id)[:3] == (2, 2, 2)


def test_legacy_payment_is_reconciled_before_refund() -> None:
    actor, _offering, customer = _business(840008, "legacy")
    _attach_first_touch(
        business_id=actor.business_id,
        customer_id=customer.id,
    )
    payment_id = str(uuid4())
    timestamp = _NOW.isoformat(timespec="microseconds")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO business_payments(
                id, business_id, customer_id, amount_minor, currency,
                status, provider, external_reference, note,
                recorded_by_member_id, created_at, updated_at, paid_at, refunded_at
            ) VALUES(?, ?, ?, 7000, 'RUB', 'paid', 'manual', NULL, '',
                     ?, ?, ?, ?, NULL)
            """,
            (
                payment_id,
                actor.business_id,
                customer.id,
                actor.membership_id,
                timestamp,
                timestamp,
                timestamp,
            ),
        )

    with get_db() as conn:
        first = (
            clientplatform_business_payment_outcomes_v1
            .reconcile_business_payment_outcomes(conn)
        )
        replay = (
            clientplatform_business_payment_outcomes_v1
            .reconcile_business_payment_outcomes(conn)
        )

    assert first.payments_scanned == 1
    assert first.paid_evidence_created == 1
    assert first.refund_evidence_created == 0
    assert replay.paid_evidence_created == 0
    assert _business_payment_counts(actor.business_id) == (1, 1, 1, 1)

    reconciled = list_payments(actor=actor)[0]
    assert reconciled.id == payment_id
    assert reconciled.idempotency_key is not None
    assert reconciled.idempotency_key.startswith("legacy-business-payment-paid:")
    assert reconciled.outcome_event_id is not None

    refunded = refund_payment(
        actor=actor,
        payment_id=payment_id,
        idempotency_key="legacy-refund",
    )
    assert refunded.status == "refunded"
    assert refunded.refund_outcome_event_id is not None
    assert _business_payment_counts(actor.business_id) == (1, 2, 2, 2)


def test_schema_and_privacy_manifest_cover_payment_evidence_bridge() -> None:
    # The manifest discovers columns through SQLite PRAGMA, which the hardened
    # read-only wrapper intentionally rejects as non-SELECT SQL.
    with get_db() as conn:
        report = validate_clientplatform_privacy_manifest(conn, strict=True)
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(business_payment_outcome_evidence)"
            ).fetchall()
        }
    assert report.ok
    assert "business_payment_outcome_evidence" in TENANT_POLICIES
    assert {
        "business_id",
        "payment_id",
        "idempotency_key",
        "request_fingerprint",
        "outcome_event_id",
    } <= columns
