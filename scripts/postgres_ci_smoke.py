from __future__ import annotations

import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.accounts.identity import ensure_account
from services.db import db
from services.db.runtime import CONFIG
from services.payments.reconciliation import ReconciliationResult
from services.payments.retry_queue import (
    claim_due_payment_retries,
    enqueue_verified_payment_retry,
)
from services.payments.telegram_stars import (
    build_stars_payload,
    record_successful_stars_payment,
)
from services.payments.telegram_stars_refunds import (
    cancel_prepared_stars_refund,
    prepare_stars_refund,
    preview_stars_refund,
)
from services.practice_token_contract import package_by_id, telegram_stars_price
from services.practice_tokens import get_wallet, grant_tokens_for_payment
from services.schema import init_db
from services.migrations.clientplatform_business_payment_outcomes_v1 import (
    reconcile_business_payment_outcomes,
)


def _enabled(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _assert_ci_guardrails() -> None:
    if not _enabled("POSTGRES_CI_SMOKE"):
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED explicit POSTGRES_CI_SMOKE=1 is required")
    if (os.getenv("APP_ENV") or "").strip().lower() in {"prod", "production"}:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED refuses production environment")
    if not CONFIG.uses_postgres:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED active engine is not Postgres")


def _exercise_payment_and_refund() -> tuple[int, str]:
    suffix = uuid.uuid4().hex
    user_id = 8_000_000_000 + (uuid.uuid4().int % 1_000_000_000)
    charge_id = f"postgres-ci-stars-{suffix}"
    package_id = "practice_start_7"
    package = package_by_id(package_id)
    payload = build_stars_payload(buyer_user_id=user_id, package_id=package_id)
    amount = telegram_stars_price(package_id)

    first = record_successful_stars_payment(
        user_id=user_id,
        payload=payload,
        total_amount=amount,
        currency="XTR",
        telegram_charge_id=charge_id,
    )
    duplicate = record_successful_stars_payment(
        user_id=user_id,
        payload=payload,
        total_amount=amount,
        currency="XTR",
        telegram_charge_id=charge_id,
    )
    if first.duplicate or not duplicate.duplicate:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED Stars idempotency contract")
    if get_wallet(user_id).available_tokens != package.tokens:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED token grant contract")

    plan = preview_stars_refund(charge_id)
    if not plan.refundable or plan.tokens != package.tokens:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED refund preflight contract")
    prepared = prepare_stars_refund(charge_id, requested_by=user_id)
    if prepared.status != "prepared" or get_wallet(user_id).available_tokens != 0:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED refund hold contract")
    cancel_prepared_stars_refund(charge_id, error="ci_provider_not_called")
    if get_wallet(user_id).available_tokens != package.tokens:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED refund hold rollback contract")
    return user_id, charge_id


def _exercise_concurrent_payment_grant() -> None:
    user_id = 8_500_000_000 + (uuid.uuid4().int % 1_000_000_000)
    payment_id = f"postgres-ci-concurrent-{uuid.uuid4().hex}"
    package_id = "practice_start_7"
    package = package_by_id(package_id)
    ensure_account(user_id)

    def grant_once() -> bool:
        inserted, _wallet, _ledger_id = grant_tokens_for_payment(
            provider="postgres_ci",
            provider_payment_id=payment_id,
            user_id=user_id,
            package_id=package_id,
            source="postgres_concurrency_probe",
        )
        return bool(inserted)

    with ThreadPoolExecutor(max_workers=4) as pool:
        inserted_flags = list(pool.map(lambda _: grant_once(), range(4)))

    if sum(1 for item in inserted_flags if item) != 1:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED concurrent payment claim contract")
    if get_wallet(user_id).available_tokens != package.tokens:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED concurrent payment wallet contract")
    with db() as conn:
        grant_count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM payment_token_grants WHERE provider=? AND provider_payment_id=?",
                ("postgres_ci", payment_id),
            ).fetchone()["n"]
        )
        lot_count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM practice_token_lots WHERE provider=? AND provider_payment_id=?",
                ("postgres_ci", payment_id),
            ).fetchone()["n"]
        )
    if grant_count != 1 or lot_count != 1:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED concurrent payment provenance contract")


def _exercise_payment_retry_claim_concurrency() -> None:
    payment_id = f"postgres-ci-payment-retry-{uuid.uuid4().hex}"
    payload = {
        "event": "payment.succeeded",
        "object": {
            "id": payment_id,
            "status": "succeeded",
            "amount": {"value": "2499.00", "currency": "RUB"},
            "metadata": {
                "project": "metrotherapy",
                "user_id": "885099",
                "source": "max",
                "kind": "tokens",
                "package_id": "practice_start_7",
            },
        },
    }
    result = ReconciliationResult(
        ok=True,
        provider="yookassa",
        provider_payment_id=payment_id,
        status="succeeded",
        event="payment.succeeded",
        inserted=False,
        problem="practice_grant_failed:RuntimeError",
        processing_status="action_required",
        side_effects_done=False,
    )
    enqueue_verified_payment_retry(payload, result)

    def claim_once() -> list[int]:
        return [item.id for item in claim_due_payment_retries(limit=1)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed_groups = list(pool.map(lambda _: claim_once(), range(4)))
    claimed_ids = [item for group in claimed_groups for item in group]
    if len(claimed_ids) != 1 or len(set(claimed_ids)) != 1:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED payment retry SKIP LOCKED contract")

    with db() as conn:
        row = conn.execute(
            "SELECT status,lock_token FROM payment_reconciliation_retry WHERE provider=? AND provider_payment_id=?",
            ("yookassa", payment_id),
        ).fetchone()
        if not row or row["status"] != "processing" or not str(row["lock_token"] or ""):
            raise SystemExit("POSTGRES_CI_SMOKE_FAILED payment retry lease persistence contract")
        conn.execute(
            "DELETE FROM payment_reconciliation_retry WHERE provider=? AND provider_payment_id=?",
            ("yookassa", payment_id),
        )


def _exercise_clientplatform_legacy_payment_backfill() -> None:
    suffix = uuid.uuid4().hex
    business_id = f"postgres-ci-business-{suffix}"
    member_id = f"postgres-ci-member-{suffix}"
    payment_id = f"postgres-ci-business-payment-{suffix}"
    user_id = 8_700_000_000 + (uuid.uuid4().int % 1_000_000_000)
    stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO businesses(
                id,name,status,created_by_user_id,created_at,updated_at
            ) VALUES(?, 'Postgres payment migration', 'active', ?, ?, ?)
            """,
            (business_id, user_id, stamp, stamp),
        )
        conn.execute(
            """
            INSERT INTO business_members(
                id,business_id,user_id,role,status,created_at,updated_at
            ) VALUES(?, ?, ?, 'owner', 'active', ?, ?)
            """,
            (member_id, business_id, user_id, stamp, stamp),
        )
        conn.execute(
            """
            INSERT INTO business_payments(
                id,business_id,customer_id,amount_minor,currency,status,
                provider,external_reference,note,recorded_by_member_id,
                created_at,updated_at,paid_at,refunded_at
            ) VALUES(?, ?, NULL, 15000, 'RUB', 'refunded', 'manual', NULL, '',
                     ?, ?, ?, ?, ?)
            """,
            (payment_id, business_id, member_id, stamp, stamp, stamp, stamp),
        )
        first = reconcile_business_payment_outcomes(conn)
        replay = reconcile_business_payment_outcomes(conn)
        evidence = int(
            conn.execute(
                """
                SELECT COUNT(*) AS n FROM business_payment_outcome_evidence
                WHERE business_id=? AND payment_id=?
                """,
                (business_id, payment_id),
            ).fetchone()["n"]
        )
        outcomes = int(
            conn.execute(
                """
                SELECT COUNT(*) AS n FROM business_outcome_events
                WHERE business_id=? AND source_type='business_payment'
                  AND source_id=?
                """,
                (business_id, payment_id),
            ).fetchone()["n"]
        )
    if (
        first.paid_evidence_created != 1
        or first.refund_evidence_created != 1
        or replay.paid_evidence_created != 0
        or replay.refund_evidence_created != 0
        or evidence != 2
        or outcomes != 2
    ):
        raise SystemExit(
            "POSTGRES_CI_SMOKE_FAILED ClientPlatform legacy payment backfill"
        )


def _assert_ledgers(charge_id: str) -> None:
    with db() as conn:
        payment = conn.execute(
            "SELECT processing_status FROM payments WHERE telegram_charge_id=?",
            (charge_id,),
        ).fetchone()
        refund = conn.execute(
            "SELECT status, attempts FROM telegram_stars_refunds WHERE telegram_charge_id=?",
            (charge_id,),
        ).fetchone()
    if not payment or payment["processing_status"] != "side_effects_done":
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED payment ledger contract")
    if not refund or refund["status"] != "failed" or int(refund["attempts"]) != 1:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED refund ledger contract")


def main() -> int:
    _assert_ci_guardrails()
    init_db()
    _, charge_id = _exercise_payment_and_refund()
    _assert_ledgers(charge_id)
    _exercise_concurrent_payment_grant()
    _exercise_payment_retry_claim_concurrency()
    _exercise_clientplatform_legacy_payment_backfill()
    print("POSTGRES_CI_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
