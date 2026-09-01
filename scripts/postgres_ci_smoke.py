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

from clientplatform.application.admin_ops import record_payment, refund_payment
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import db
from services.db.runtime import CONFIG
from services.migrations.clientplatform_business_payment_outcomes_v1 import (
    reconcile_business_payment_outcomes,
)
from services.schema import init_db


def _enabled(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _assert_ci_guardrails() -> None:
    if not _enabled("POSTGRES_CI_SMOKE"):
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED explicit POSTGRES_CI_SMOKE=1 is required")
    if (os.getenv("APP_ENV") or "").strip().lower() in {"prod", "production"}:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED refuses production environment")
    if not CONFIG.uses_postgres:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED active engine is not Postgres")


def _exercise_business_payment_concurrency() -> None:
    suffix = uuid.uuid4().hex
    business_id = str(uuid.uuid4())
    owner_user_id = 8_100_000_000 + (uuid.uuid4().int % 100_000_000)
    stamp = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    with db() as conn:
        tenancy = TenancyRepository(conn)
        tenancy.create_business(
            owner_user_id=owner_user_id,
            name=f"Postgres canonical money smoke {suffix[:8]}",
            business_id=business_id,
            now=stamp.isoformat(),
        )
        actor = tenancy.resolve_context(user_id=owner_user_id, business_id=business_id)

    paid_key = f"postgres-ci-paid-{suffix}"

    def record_once():
        return record_payment(
            actor=actor,
            amount_minor=15_000,
            currency="RUB",
            idempotency_key=paid_key,
            provider="manual",
            note="postgres canonical money smoke",
            now=stamp,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        payments = list(pool.map(lambda _: record_once(), range(4)))
    payment_ids = {item.id for item in payments}
    if len(payment_ids) != 1:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED business payment idempotency")
    payment_id = next(iter(payment_ids))

    refund_key = f"postgres-ci-refund-{suffix}"
    refund_stamp = datetime(2026, 8, 31, 12, 1, tzinfo=timezone.utc)

    def refund_once():
        return refund_payment(
            actor=actor,
            payment_id=payment_id,
            idempotency_key=refund_key,
            provider="manual",
            reason="postgres canonical refund smoke",
            now=refund_stamp,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        refunds = list(pool.map(lambda _: refund_once(), range(4)))
    if {item.id for item in refunds} != {payment_id} or any(item.status != "refunded" for item in refunds):
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED business refund idempotency")

    with db() as conn:
        payment_count = int(conn.execute(
            "SELECT COUNT(*) AS n FROM business_payments WHERE business_id=? AND id=?",
            (business_id, payment_id),
        ).fetchone()["n"])
        evidence_count = int(conn.execute(
            "SELECT COUNT(*) AS n FROM business_payment_outcome_evidence WHERE business_id=? AND payment_id=?",
            (business_id, payment_id),
        ).fetchone()["n"])
        outcome_count = int(conn.execute(
            "SELECT COUNT(*) AS n FROM business_outcome_events WHERE business_id=? AND source_type='business_payment' AND source_id=?",
            (business_id, payment_id),
        ).fetchone()["n"])
    if payment_count != 1 or evidence_count != 2 or outcome_count != 2:
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED business money ledger contract")


def _exercise_business_payment_backfill() -> None:
    suffix = uuid.uuid4().hex
    business_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    payment_id = str(uuid.uuid4())
    user_id = 8_700_000_000 + (uuid.uuid4().int % 1_000_000_000)
    stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with db() as conn:
        conn.execute(
            "INSERT INTO businesses(id,name,status,created_by_user_id,created_at,updated_at) VALUES(?, ?, 'active', ?, ?, ?)",
            (business_id, f"Postgres payment backfill {suffix[:8]}", user_id, stamp, stamp),
        )
        conn.execute(
            "INSERT INTO business_members(id,business_id,user_id,role,status,created_at,updated_at) VALUES(?, ?, ?, 'owner', 'active', ?, ?)",
            (member_id, business_id, user_id, stamp, stamp),
        )
        conn.execute(
            """
            INSERT INTO business_payments(
                id,business_id,customer_id,amount_minor,currency,status,
                provider,external_reference,note,recorded_by_member_id,
                created_at,updated_at,paid_at,refunded_at
            ) VALUES(?, ?, NULL, 15000, 'RUB', 'refunded', 'manual', NULL, '', ?, ?, ?, ?, ?)
            """,
            (payment_id, business_id, member_id, stamp, stamp, stamp, stamp),
        )
        first = reconcile_business_payment_outcomes(conn)
        replay = reconcile_business_payment_outcomes(conn)
        evidence = int(conn.execute(
            "SELECT COUNT(*) AS n FROM business_payment_outcome_evidence WHERE business_id=? AND payment_id=?",
            (business_id, payment_id),
        ).fetchone()["n"])
        outcomes = int(conn.execute(
            "SELECT COUNT(*) AS n FROM business_outcome_events WHERE business_id=? AND source_type='business_payment' AND source_id=?",
            (business_id, payment_id),
        ).fetchone()["n"])
    if (
        first.paid_evidence_created != 1
        or first.refund_evidence_created != 1
        or replay.paid_evidence_created != 0
        or replay.refund_evidence_created != 0
        or evidence != 2
        or outcomes != 2
    ):
        raise SystemExit("POSTGRES_CI_SMOKE_FAILED business payment backfill")


def main() -> int:
    _assert_ci_guardrails()
    init_db()
    _exercise_business_payment_concurrency()
    _exercise_business_payment_backfill()
    print("POSTGRES_CI_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
