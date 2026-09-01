from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clientplatform.domain.bookings import BookingInvariantViolation
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from services.db import get_connection, get_db
from services.db.core import PostgresCompatConnection
from services.db.runtime import CONFIG
from services.schema import init_db


class _SynchronizedConnection(PostgresCompatConnection):
    """Expose race windows on old code while synchronizing the new lock path."""

    def __init__(
        self,
        delegate: Any,
        *,
        lock_gate: threading.Barrier,
        legacy_gate: threading.Barrier,
        legacy_predicate: Callable[[str], bool],
    ) -> None:
        self._delegate = delegate
        self._lock_gate = lock_gate
        self._legacy_gate = legacy_gate
        self._legacy_predicate = legacy_predicate
        self._lock_seen = False

    def execute(self, sql: str, params: Any = ()) -> Any:
        compact = " ".join(str(sql).lower().split())
        if "pg_advisory_xact_lock" in compact:
            self._lock_gate.wait(timeout=15)
            self._lock_seen = True
        elif not self._lock_seen and self._legacy_predicate(compact):
            self._legacy_gate.wait(timeout=15)
        return self._delegate.execute(sql, params)


def _run_pair(worker: Callable[[int], str]) -> list[str]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(worker, (0, 1)))


def _cleanup_business(business_id: str) -> None:
    """Delete probe-owned rows in FK-safe order for reusable staging databases."""

    with get_db() as conn:
        for table in (
            "booking_slots",
            "customer_invites",
            "business_offerings",
            "business_capabilities",
            "business_profiles",
            "customer_identities",
            "customers",
        ):
            conn.execute(f"DELETE FROM {table} WHERE business_id=?", (business_id,))
        conn.execute("DELETE FROM businesses WHERE id=?", (business_id,))


def main() -> int:
    if not CONFIG.uses_postgres:
        raise SystemExit("POSTGRES_BOOKING_CONCURRENCY_FAILED: CLIENTPLATFORM_DB_ENGINE=postgres is required")
    if not (os.getenv("DATABASE_URL") or "").strip():
        raise SystemExit("POSTGRES_BOOKING_CONCURRENCY_FAILED: DATABASE_URL is required")
    if (os.getenv("POSTGRES_REUSE_CONNECTIONS") or "").strip() != "0":
        raise SystemExit(
            "POSTGRES_BOOKING_CONCURRENCY_FAILED: POSTGRES_REUSE_CONNECTIONS=0 is required "
            "to prove two independent PostgreSQL connections"
        )

    init_db()
    suffix = uuid.uuid4().hex[:12]
    owner_user_id = 9_300_000_000 + int(suffix[:6], 16)
    telegram_user_id = owner_user_id + 1
    business_id = ""
    try:
        with get_db() as conn:
            tenancy = TenancyRepository(conn)
            activity = ActivityRepository(conn)
            business = tenancy.create_business(
                owner_user_id=owner_user_id,
                name=f"Concurrency {suffix}",
            )
            business_id = business.business.id
            owner = tenancy.resolve_context(user_id=owner_user_id, business_id=business_id)
            activity.upsert_profile(
                actor=owner,
                activity_description="PostgreSQL booking concurrency probe",
                timezone_name="Europe/Amsterdam",
                now="2026-07-28T12:00:00+00:00",
            )
            consultations = activity.enable_capability(
                actor=owner,
                connector_key="consultations",
                now="2026-07-28T12:00:00+00:00",
            )
            services = activity.enable_capability(
                actor=owner,
                connector_key="services",
                now="2026-07-28T12:00:00+00:00",
            )
            offering_a = activity.create_offering(
                actor=owner,
                capability_id=consultations.id,
                title="Consultation",
                description="Concurrency probe A",
                now="2026-07-28T12:00:00+00:00",
            )
            offering_b = activity.create_offering(
                actor=owner,
                capability_id=services.id,
                title="Service",
                description="Concurrency probe B",
                now="2026-07-28T12:00:00+00:00",
            )
            invite = activity.issue_customer_invite(
                actor=owner,
                now="2026-07-28T12:00:00+00:00",
            )
            claim = activity.claim_customer_invite(
                token=invite.token,
                telegram_user_id=telegram_user_id,
                username=f"postgres_{suffix}",
                display_name="Postgres booking probe",
                now="2026-07-28T12:05:00+00:00",
            )

        slot_lock_gate = threading.Barrier(2)
        slot_legacy_gate = threading.Barrier(2)
        slot_starts = ("02.08.2030 10:00", "02.08.2030 10:30")

        def publish(index: int) -> str:
            with get_connection() as raw:
                conn = _SynchronizedConnection(
                    raw,
                    lock_gate=slot_lock_gate,
                    legacy_gate=slot_legacy_gate,
                    legacy_predicate=lambda sql: (
                        "from booking_slots" in sql
                        and "offering_id" in sql
                        and "status in ('open', 'booked')" in sql
                    ),
                )
                try:
                    BookingRepository(conn).create_slot(
                        actor=owner,
                        offering_id=offering_a.id,
                        local_start=slot_starts[index],
                        duration_minutes=60,
                        now="2026-07-28T12:00:00+00:00",
                    )
                except BookingInvariantViolation:
                    return "conflict"
                return "created"

        slot_results = _run_pair(publish)
        assert sorted(slot_results) == ["conflict", "created"], slot_results

        with get_db() as conn:
            repo = BookingRepository(conn)
            booking_a = repo.create_slot(
                actor=owner,
                offering_id=offering_a.id,
                local_start="03.08.2030 10:00",
                duration_minutes=60,
                now="2026-07-28T12:00:00+00:00",
            )
            booking_b = repo.create_slot(
                actor=owner,
                offering_id=offering_b.id,
                local_start="03.08.2030 10:30",
                duration_minutes=60,
                now="2026-07-28T12:00:00+00:00",
            )

        booking_lock_gate = threading.Barrier(2)
        booking_legacy_gate = threading.Barrier(2)
        slot_ids = (booking_a.slot.id, booking_b.slot.id)

        def book(index: int) -> str:
            with get_connection() as raw:
                conn = _SynchronizedConnection(
                    raw,
                    lock_gate=booking_lock_gate,
                    legacy_gate=booking_legacy_gate,
                    legacy_predicate=lambda sql: (
                        "from booking_slots" in sql
                        and "booked_customer_id" in sql
                        and "status='booked'" in sql
                    ),
                )
                try:
                    BookingRepository(conn).book_slot(
                        telegram_user_id=telegram_user_id,
                        business_id=business_id,
                        slot_id=slot_ids[index],
                        now="2026-07-28T12:10:00+00:00",
                    )
                except BookingInvariantViolation:
                    return "conflict"
                return "booked"

        booking_results = _run_pair(book)
        assert sorted(booking_results) == ["booked", "conflict"], booking_results

        with get_db() as conn:
            published_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM booking_slots WHERE offering_id=?",
                    (offering_a.id,),
                ).fetchone()["c"]
            )
            customer_booking_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM booking_slots
                    WHERE business_id=? AND booked_customer_id=? AND status='booked'
                    """,
                    (business_id, claim.customer_id),
                ).fetchone()["c"]
            )
        assert published_count == 2, published_count
        assert customer_booking_count == 1, customer_booking_count

        print(
            json.dumps(
                {
                    "ok": True,
                    "probe": "clientplatform_postgres_booking_concurrency",
                    "connections_per_race": 2,
                    "slot_publication": slot_results,
                    "customer_booking": booking_results,
                },
                sort_keys=True,
            )
        )
        print("POSTGRES_BOOKING_CONCURRENCY_OK")
        return 0
    finally:
        if business_id:
            _cleanup_business(business_id)


if __name__ == "__main__":
    raise SystemExit(main())
