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

    def execute(self, sql: str, params: Any = ()) -> Any:
        if "pg_advisory_xact_lock" in " ".join(str(sql).lower().split()):
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


def main() -> int:
    if not CONFIG.uses_postgres:
        raise SystemExit("POSTGRES_BOT_GATEWAY_FAILED: METRO_DB_ENGINE=postgres is required")
    if not (os.getenv("DATABASE_URL") or "").strip():
        raise SystemExit("POSTGRES_BOT_GATEWAY_FAILED: DATABASE_URL is required")
    if (os.getenv("POSTGRES_REUSE_CONNECTIONS") or "").strip() != "0":
        raise SystemExit(
            "POSTGRES_BOT_GATEWAY_FAILED: POSTGRES_REUSE_CONNECTIONS=0 is required"
        )

    init_db()
    suffix = uuid.uuid4().hex[:12]
    owner_user_id = 9_500_000_000 + int(suffix[:6], 16)
    business_id = ""
    try:
        with get_db() as conn:
            tenancy = TenancyRepository(conn)
            connections = ConnectionRepository(conn)
            access = tenancy.create_business(
                owner_user_id=owner_user_id,
                name=f"Bot Gateway {suffix}",
            )
            business_id = access.business.id
            owner = tenancy.resolve_context(
                user_id=owner_user_id,
                business_id=business_id,
            )
            connection = connections.create_connection(
                actor=owner,
                platform="telegram",
                connection_type="telegram_managed_bot",
                external_account_id=str(owner_user_id + 1000),
                credential_reference=(
                    "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_GATEWAY_PROBE"
                ),
            )
            connection = connections.activate_connection(
                actor=owner,
                connection_id=connection.id,
            )
            managed = connections.register_managed_bot(
                actor=owner,
                connection_id=connection.id,
                external_bot_id=str(owner_user_id + 1000),
                webhook_secret_reference=(
                    "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_GATEWAY_PROBE"
                ),
            )
            route = BotGatewayRepository(conn).resolve_telegram_route(
                external_bot_id=managed.external_bot_id
            )

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

        with get_db() as conn:
            BotGatewayRepository(conn).admit_telegram_update(
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
                (business_id,),
            ).fetchall()
        status_counts = {str(row["status"]): int(row["c"]) for row in counts}
        assert status_counts.get("processing") == 1, status_counts
        assert sum(status_counts.values()) == 3, status_counts

        print(
            json.dumps(
                {
                    "ok": True,
                    "probe": "clientplatform_postgres_bot_gateway_concurrency",
                    "connections_per_race": 2,
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
        if business_id:
            with get_db() as conn:
                conn.execute("DELETE FROM businesses WHERE id=?", (business_id,))


if __name__ == "__main__":
    raise SystemExit(main())
