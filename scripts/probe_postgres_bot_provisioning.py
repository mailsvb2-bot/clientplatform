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

from clientplatform.domain.bot_provisioning import BotProvisioningInvariantViolation
from clientplatform.infrastructure import BotProvisioningRepository, TenancyRepository
from services.db import get_connection, get_db
from services.db.core import PostgresCompatConnection
from services.db.runtime import CONFIG
from services.schema import init_db


class _BarrierConnection(PostgresCompatConnection):
    def __init__(
        self,
        delegate: Any,
        *,
        gate: threading.Barrier,
        marker: str,
    ) -> None:
        self._delegate = delegate
        self._gate = gate
        self._marker = " ".join(marker.lower().split())
        self._waiting = True

    def execute(self, sql: str, params: Any = ()) -> Any:
        normalized = " ".join(str(sql).lower().split())
        if self._waiting and self._marker in normalized:
            self._waiting = False
            self._gate.wait(timeout=15)
        return self._delegate.execute(sql, params)


def _run_pair(worker: Callable[[int], str]) -> list[str]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(worker, (0, 1)))


def main() -> int:
    if not CONFIG.uses_postgres:
        raise SystemExit(
            "POSTGRES_BOT_PROVISIONING_FAILED: CLIENTPLATFORM_DB_ENGINE=postgres is required"
        )
    if not (os.getenv("DATABASE_URL") or "").strip():
        raise SystemExit("POSTGRES_BOT_PROVISIONING_FAILED: DATABASE_URL is required")
    if (os.getenv("POSTGRES_REUSE_CONNECTIONS") or "").strip() != "0":
        raise SystemExit(
            "POSTGRES_BOT_PROVISIONING_FAILED: POSTGRES_REUSE_CONNECTIONS=0 is required"
        )

    init_db()
    suffix = uuid.uuid4().hex[:12]
    owner_user_id = 9_700_000_000 + int(suffix[:6], 16)
    business_id = ""
    try:
        with get_db() as conn:
            tenancy = TenancyRepository(conn)
            access = tenancy.create_business(
                owner_user_id=owner_user_id,
                name=f"Bot Provisioning {suffix}",
            )
            business_id = access.business.id
            owner = tenancy.resolve_context(
                user_id=owner_user_id,
                business_id=business_id,
            )

        create_gate = threading.Barrier(2)

        def create_same(_index: int) -> str:
            with get_connection() as raw:
                conn = _BarrierConnection(
                    raw,
                    gate=create_gate,
                    marker="pg_advisory_xact_lock",
                )
                request = BotProvisioningRepository(conn).create_request(
                    actor=owner,
                    idempotency_key=f"probe-{suffix}-same",
                    requested_username="provisioning_probe_bot",
                    display_name="Provisioning Probe",
                )
                return request.id

        create_results = _run_pair(create_same)
        assert len(set(create_results)) == 1, create_results

        with get_db() as conn:
            repo = BotProvisioningRepository(conn)
            ready = repo.create_request(
                actor=owner,
                idempotency_key=f"probe-{suffix}-lease",
                requested_username="provisioning_probe_bot",
            )
            ready = repo.submit_secret_references(
                actor=owner,
                request_id=ready.id,
                credential_reference=(
                    "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_PROVISIONING_PROBE"
                ),
                webhook_secret_reference=(
                    "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_PROVISIONING_PROBE"
                ),
            )

        lease_gate = threading.Barrier(2)

        def claim_ready(_index: int) -> str:
            with get_connection() as raw:
                conn = _BarrierConnection(
                    raw,
                    gate=lease_gate,
                    marker="SET status='verifying'",
                )
                try:
                    lease = BotProvisioningRepository(conn).begin_verification(
                        actor=owner,
                        request_id=ready.id,
                        now="2026-07-29T09:10:00+00:00",
                    )
                except BotProvisioningInvariantViolation:
                    return "conflict"
                return f"leased:{lease.verification_token}"

        lease_results = _run_pair(claim_ready)
        assert sum(value.startswith("leased:") for value in lease_results) == 1, lease_results
        assert lease_results.count("conflict") == 1, lease_results

        with get_db() as conn:
            stale_repo = BotProvisioningRepository(conn)
            stale = stale_repo.create_request(
                actor=owner,
                idempotency_key=f"probe-{suffix}-stale",
                requested_username="stale_provisioning_probe_bot",
            )
            stale = stale_repo.submit_secret_references(
                actor=owner,
                request_id=stale.id,
                credential_reference=(
                    "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_STALE_PROBE"
                ),
                webhook_secret_reference=(
                    "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_STALE_PROBE"
                ),
            )
            first_lease = stale_repo.begin_verification(
                actor=owner,
                request_id=stale.id,
                now="2026-07-29T09:00:00+00:00",
            )

        stale_gate = threading.Barrier(2)

        def recover_stale(_index: int) -> str:
            with get_connection() as raw:
                conn = _BarrierConnection(
                    raw,
                    gate=stale_gate,
                    marker="SET status='ready'",
                )
                try:
                    lease = BotProvisioningRepository(conn).begin_verification(
                        actor=owner,
                        request_id=stale.id,
                        now="2026-07-29T09:10:00+00:00",
                        stale_after_seconds=300,
                    )
                except BotProvisioningInvariantViolation:
                    return "conflict"
                return f"recovered:{lease.verification_token}"

        stale_results = _run_pair(recover_stale)
        assert sum(value.startswith("recovered:") for value in stale_results) == 1, stale_results
        assert stale_results.count("conflict") == 1, stale_results
        recovered_token = next(
            value.split(":", 1)[1]
            for value in stale_results
            if value.startswith("recovered:")
        )
        assert recovered_token != first_lease.verification_token

        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS c
                FROM managed_bot_provisioning_requests
                WHERE business_id=?
                GROUP BY status
                """,
                (business_id,),
            ).fetchall()
        status_counts = {str(row["status"]): int(row["c"]) for row in rows}
        assert status_counts.get("awaiting_secret") == 1, status_counts
        assert status_counts.get("verifying") == 2, status_counts

        print(
            json.dumps(
                {
                    "ok": True,
                    "probe": "clientplatform_postgres_bot_provisioning",
                    "connections_per_race": 2,
                    "same_request": create_results,
                    "fresh_lease": lease_results,
                    "stale_lease": stale_results,
                    "status_counts": status_counts,
                },
                sort_keys=True,
            )
        )
        print("POSTGRES_BOT_PROVISIONING_OK")
        return 0
    finally:
        if business_id:
            with get_db() as conn:
                conn.execute("DELETE FROM businesses WHERE id=?", (business_id,))


if __name__ == "__main__":
    raise SystemExit(main())
