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

from clientplatform.domain.bot_gateway import BotGatewayReplayConflict
from clientplatform.domain.connections import ConnectionInvariantViolation
from clientplatform.infrastructure import ConnectionRepository, TenancyRepository
from clientplatform.infrastructure.safe_bot_gateway_repository import BotGatewayRepository
from services.db import get_connection, get_db
from services.db.core import PostgresCompatConnection
from services.db.runtime import CONFIG
from services.schema import init_db


class _AdmissionConnection(PostgresCompatConnection):
    def __init__(self, delegate: Any, *, gate: threading.Barrier) -> None:
        self._delegate = delegate
        self._gate = gate
        self._waiting = True

    def execute(self, sql: str, params: Any = ()) -> Any:
        if (
            self._waiting
            and "pg_advisory_xact_lock" in " ".join(str(sql).lower().split())
        ):
            self._waiting = False
            self._gate.wait(timeout=15)
        return self._delegate.execute(sql, params)


def _run_pair(worker: Callable[[int], str]) -> list[str]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(worker, (0, 1)))


def _payload(update_id: int, text: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1_700_000_000,
            "chat": {"id": 88001, "type": "private"},
            "from": {"id": 88001, "is_bot": False, "first_name": "Probe"},
            "text": text,
        },
    }


def _create_active_connection(
    conn: Any,
    *,
    owner: Any,
    external_bot_id: str,
    suffix: str,
):
    connections = ConnectionRepository(conn)
    connection = connections.create_connection(
        actor=owner,
        platform="telegram",
        connection_type="telegram_managed_bot",
        external_account_id=external_bot_id,
        credential_reference=(
            f"secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_GATEWAY_{suffix}"
        ),
    )
    return connections.activate_connection(
        actor=owner,
        connection_id=connection.id,
    )


def main() -> int:
    if not CONFIG.uses_postgres:
        raise SystemExit("POSTGRES_BOT_GATEWAY_FAILED: CLIENTPLATFORM_DB_ENGINE=postgres is required")
    if not (os.getenv("DATABASE_URL") or "").strip():
        raise SystemExit("POSTGRES_BOT_GATEWAY_FAILED: DATABASE_URL is required")
    if (os.getenv("POSTGRES_REUSE_CONNECTIONS") or "").strip() != "0":
        raise SystemExit(
            "POSTGRES_BOT_GATEWAY_FAILED: POSTGRES_REUSE_CONNECTIONS=0 is required"
        )

    init_db()
    suffix = uuid.uuid4().hex[:12]
    owner_user_id = 9_500_000_000 + int(suffix[:6], 16)
    business_ids: list[str] = []
    try:
        with get_db() as conn:
            tenancy = TenancyRepository(conn)
            access = tenancy.create_business(
                owner_user_id=owner_user_id,
                name=f"Bot Gateway {suffix}",
            )
            business_ids.append(access.business.id)
            owner = tenancy.resolve_context(
                user_id=owner_user_id,
                business_id=access.business.id,
            )
            connection = _create_active_connection(
                conn,
                owner=owner,
                external_bot_id=str(owner_user_id + 1000),
                suffix="PRIMARY",
            )
            managed = ConnectionRepository(conn).register_managed_bot(
                actor=owner,
                connection_id=connection.id,
                external_bot_id=str(owner_user_id + 1000),
                webhook_secret_reference=(
                    "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_GATEWAY_PRIMARY"
                ),
            )
            route = BotGatewayRepository(conn).resolve_telegram_route(
                external_bot_id=managed.external_bot_id
            )

            retry_access = tenancy.create_business(
                owner_user_id=owner_user_id + 1,
                name=f"Bot Retry {suffix}",
            )
            business_ids.append(retry_access.business.id)
            retry_owner = tenancy.resolve_context(
                user_id=owner_user_id + 1,
                business_id=retry_access.business.id,
            )
            retry_connection = _create_active_connection(
                conn,
                owner=retry_owner,
                external_bot_id=str(owner_user_id + 2000),
                suffix="RETRY",
            )

            conflict_access = tenancy.create_business(
                owner_user_id=owner_user_id + 2,
                name=f"Bot Conflict {suffix}",
            )
            business_ids.append(conflict_access.business.id)
            conflict_owner = tenancy.resolve_context(
                user_id=owner_user_id + 2,
                business_id=conflict_access.business.id,
            )
            conflict_connections = (
                _create_active_connection(
                    conn,
                    owner=conflict_owner,
                    external_bot_id=str(owner_user_id + 3000),
                    suffix="CONFLICT_A",
                ),
                _create_active_connection(
                    conn,
                    owner=conflict_owner,
                    external_bot_id=str(owner_user_id + 3001),
                    suffix="CONFLICT_B",
                ),
            )

        registration_gate = threading.Barrier(2)

        def register_same(_index: int) -> str:
            with get_connection() as raw:
                conn = _AdmissionConnection(raw, gate=registration_gate)
                bot = ConnectionRepository(conn).register_managed_bot(
                    actor=retry_owner,
                    connection_id=retry_connection.id,
                    external_bot_id=retry_connection.external_account_id,
                    webhook_secret_reference=(
                        "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_GATEWAY_RETRY"
                    ),
                )
                return bot.id

        registration_results = _run_pair(register_same)
        assert len(set(registration_results)) == 1, registration_results
        with get_db() as conn:
            registration_count = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM managed_bots
                WHERE business_id=? AND platform='telegram'
                """,
                (retry_owner.business_id,),
            ).fetchone()
        assert int(registration_count["c"]) == 1, registration_count

        business_gate = threading.Barrier(2)

        def register_different(index: int) -> str:
            selected = conflict_connections[index]
            with get_connection() as raw:
                conn = _AdmissionConnection(raw, gate=business_gate)
                try:
                    ConnectionRepository(conn).register_managed_bot(
                        actor=conflict_owner,
                        connection_id=selected.id,
                        external_bot_id=selected.external_account_id,
                        webhook_secret_reference=(
                            f"secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_GATEWAY_CONFLICT_{index}"
                        ),
                    )
                except ConnectionInvariantViolation:
                    return "conflict"
                return "created"

        business_results = _run_pair(register_different)
        assert sorted(business_results) == ["conflict", "created"], business_results

        same_gate = threading.Barrier(2)

        def admit_same(_index: int) -> str:
            with get_connection() as raw:
                conn = _AdmissionConnection(raw, gate=same_gate)
                result = BotGatewayRepository(conn).admit_telegram_update(
                    route=route,
                    provider_update_id=100,
                    payload=_payload(100, "same"),
                )
                return "duplicate" if result.duplicate else "created"

        same_results = _run_pair(admit_same)
        assert sorted(same_results) == ["created", "duplicate"], same_results

        conflict_gate = threading.Barrier(2)

        def admit_conflict(index: int) -> str:
            with get_connection() as raw:
                conn = _AdmissionConnection(raw, gate=conflict_gate)
                try:
                    result = BotGatewayRepository(conn).admit_telegram_update(
                        route=route,
                        provider_update_id=101,
                        payload=_payload(101, f"payload-{index}"),
                    )
                except BotGatewayReplayConflict:
                    return "conflict"
                return "duplicate" if result.duplicate else "created"

        conflict_results = _run_pair(admit_conflict)
        assert sorted(conflict_results) == ["conflict", "created"], conflict_results

        # The claim race must contain exactly one due event. Terminalize the replay
        # fixtures first; otherwise two workers correctly claim two different rows.
        with get_db() as conn:
            gateway = BotGatewayRepository(conn)
            for previous in gateway.claim_due(limit=100):
                gateway.mark_processed(previous)
            gateway.admit_telegram_update(
                route=route,
                provider_update_id=102,
                payload=_payload(102, "claim"),
            )

        claim_gate = threading.Barrier(2)

        def claim(_index: int) -> str:
            claim_gate.wait(timeout=15)
            with get_connection() as raw:
                items = BotGatewayRepository(raw).claim_due(limit=1)
                return "claimed" if items else "empty"

        claim_results = _run_pair(claim)
        assert sorted(claim_results) == ["claimed", "empty"], claim_results

        with get_db() as conn:
            counts = conn.execute(
                """
                SELECT status, COUNT(*) AS c
                FROM bot_gateway_ingress_events
                WHERE business_id=?
                GROUP BY status
                """,
                (owner.business_id,),
            ).fetchall()
        status_counts = {str(row["status"]): int(row["c"]) for row in counts}
        assert status_counts.get("processing") == 1, status_counts
        assert status_counts.get("processed") == 2, status_counts
        assert sum(status_counts.values()) == 3, status_counts

        print(
            json.dumps(
                {
                    "ok": True,
                    "probe": "clientplatform_postgres_bot_gateway_concurrency",
                    "connections_per_race": 2,
                    "same_registration": registration_results,
                    "competing_registration": business_results,
                    "same_replay": same_results,
                    "conflicting_replay": conflict_results,
                    "claim": claim_results,
                    "status_counts": status_counts,
                },
                sort_keys=True,
            )
        )
        print("POSTGRES_BOT_GATEWAY_CONCURRENCY_OK")
        return 0
    finally:
        for business_id in reversed(business_ids):
            with get_db() as conn:
                conn.execute("DELETE FROM businesses WHERE id=?", (business_id,))


if __name__ == "__main__":
    raise SystemExit(main())
