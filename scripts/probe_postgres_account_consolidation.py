from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clientplatform.infrastructure import TenancyRepository
from services.accounts import consolidation
from services.accounts.identity import (
    link_channel_to_account,
    resolve_canonical_account_id,
    resolve_canonical_user_id,
)
from services.db import get_db
from services.db.runtime import CONFIG
from services.schema import init_db


def _enabled(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _guard() -> None:
    if not _enabled("POSTGRES_ACCOUNT_CONSOLIDATION_SMOKE"):
        raise SystemExit("POSTGRES_ACCOUNT_CONSOLIDATION_FAILED explicit guard is required")
    if (os.getenv("APP_ENV") or "").strip().lower() in {"prod", "production"}:
        raise SystemExit("POSTGRES_ACCOUNT_CONSOLIDATION_FAILED refuses production")
    if not CONFIG.uses_postgres:
        raise SystemExit("POSTGRES_ACCOUNT_CONSOLIDATION_FAILED engine is not Postgres")
    if (os.getenv("POSTGRES_REUSE_CONNECTIONS") or "").strip() != "0":
        raise SystemExit("POSTGRES_ACCOUNT_CONSOLIDATION_FAILED requires independent connections")


def _run_pair(worker):
    gate = threading.Barrier(2)

    def wrapped(index: int):
        gate.wait(timeout=15)
        return worker(index)

    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(wrapped, (0, 1)))


def _exercise() -> dict[str, object]:
    suffix = uuid.uuid4().hex[:10]
    source = 8_607_000_000 + (uuid.uuid4().int % 10_000_000)
    target = 8_617_000_000 + (uuid.uuid4().int % 10_000_000)
    operator = 9_607_000_000 + (uuid.uuid4().int % 10_000_000)
    when = datetime(2026, 9, 3, 15, 0, tzinfo=UTC)
    business_id = ""
    operation_key = f"pg-account-merge-{suffix}"
    original_auth = consolidation.is_platform_admin
    try:
        link_channel_to_account(target, "telegram", str(target), verified=True, link_source="pg_probe")
        link_channel_to_account(source, "vk", f"vk-{source}", verified=True, link_source="pg_probe")
        with get_db() as conn:
            tenancy = TenancyRepository(conn)
            access = tenancy.create_business(
                owner_user_id=source,
                name=f"Account Consolidation {suffix}",
                now=when.isoformat(),
            )
            business_id = access.business.id
            membership_id = access.membership.id

        consolidation.is_platform_admin = lambda user_id: int(user_id or 0) == operator
        plan = consolidation.plan_account_consolidation(
            operator,
            source_account_id=source,
            target_account_id=target,
            now_utc=when,
        )
        if not plan.can_apply or len(plan.access_expansions) != 1:
            raise AssertionError((plan.blockers, plan.access_expansions))

        def apply_once(_index: int):
            return consolidation.apply_account_consolidation(
                operator,
                source_account_id=source,
                target_account_id=target,
                expected_plan_fingerprint=plan.plan_fingerprint,
                confirmation_code=plan.confirmation_code,
                idempotency_key=operation_key,
                reason="PostgreSQL concurrency probe duplicate account",
                now_utc=when,
            )

        results = _run_pair(apply_once)
        if sum(result.idempotent_replay is False for result in results) != 1:
            raise AssertionError(results)
        if sum(result.idempotent_replay is True for result in results) != 1:
            raise AssertionError(results)
        if len({result.operation_id for result in results}) != 1:
            raise AssertionError(results)
        if resolve_canonical_account_id(source) != target:
            raise AssertionError("source account did not resolve to target")
        if resolve_canonical_user_id(source) != target:
            raise AssertionError("source user did not resolve to target")

        with get_db() as conn:
            identity_rows = conn.execute(
                "SELECT platform, account_id FROM account_channel_identities "
                "WHERE account_id=? ORDER BY platform",
                (target,),
            ).fetchall()
            membership = conn.execute(
                "SELECT id, user_id FROM business_members WHERE id=?",
                (membership_id,),
            ).fetchone()
            operation_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM account_consolidation_operations "
                    "WHERE operator_user_id=? AND idempotency_key=?",
                    (operator, operation_key),
                ).fetchone()["c"]
            )
            audit_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM account_consolidation_audit_events "
                    "WHERE operation_id=?",
                    (results[0].operation_id,),
                ).fetchone()["c"]
            )
        platforms = [str(row["platform"]) for row in identity_rows]
        if platforms != ["telegram", "vk"]:
            raise AssertionError(platforms)
        if membership is None or int(membership["user_id"]) != target:
            raise AssertionError(membership)
        if operation_count != 1 or audit_count != 1:
            raise AssertionError((operation_count, audit_count))

        return {
            "ok": True,
            "probe": "clientplatform_postgres_account_consolidation",
            "operation_id": results[0].operation_id,
            "replays": sum(result.idempotent_replay for result in results),
            "operation_rows": operation_count,
            "audit_rows": audit_count,
            "platforms": platforms,
            "membership_repointed": True,
        }
    finally:
        consolidation.is_platform_admin = original_auth
        with get_db() as conn:
            conn.execute(
                "DELETE FROM account_consolidation_audit_events WHERE operation_id IN ("
                "SELECT id FROM account_consolidation_operations "
                "WHERE operator_user_id=? AND idempotency_key=?)",
                (operator, operation_key),
            )
            conn.execute(
                "DELETE FROM account_consolidation_operations "
                "WHERE operator_user_id=? AND idempotency_key=?",
                (operator, operation_key),
            )
            if business_id:
                conn.execute("DELETE FROM businesses WHERE id=?", (business_id,))
            conn.execute(
                "DELETE FROM account_channel_identities WHERE account_id IN (?,?)",
                (source, target),
            )
            conn.execute(
                "DELETE FROM user_channel_identities WHERE user_id IN (?,?)",
                (source, target),
            )
            conn.execute(
                "DELETE FROM user_channel_preferences WHERE user_id IN (?,?)",
                (source, target),
            )
            conn.execute(
                "DELETE FROM user_channel_bridge_tokens "
                "WHERE user_id IN (?,?) OR account_id IN (?,?)",
                (source, target, source, target),
            )
            conn.execute(
                "DELETE FROM accounts WHERE account_id IN (?,?)",
                (source, target),
            )


def main() -> int:
    _guard()
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_merge_log(
                id BIGSERIAL PRIMARY KEY,
                target_account_id INTEGER NOT NULL,
                source_account_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """.strip()
        )
    init_db()
    with get_db() as conn:
        legacy = conn.execute(
            "SELECT 1 AS present FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_name='account_merge_log'"
        ).fetchone()
        if legacy is not None:
            raise AssertionError("legacy account_merge_log survived canonical migrations")
    evidence = _exercise()
    print(json.dumps(evidence, sort_keys=True))
    print("POSTGRES_ACCOUNT_CONSOLIDATION_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
