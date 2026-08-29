from __future__ import annotations

import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clientplatform.domain.automation_policy import (
    AutomationMode,
    AutomationPolicyConflict,
    AutomationPolicySpec,
    AutomationSchedule,
)
from clientplatform.infrastructure.automation_policy_repository import AutomationPolicyRepository
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository
from services.db import db
from services.db.runtime import CONFIG
from services.schema import init_db

_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def _spec(mode: AutomationMode) -> AutomationPolicySpec:
    return AutomationPolicySpec(
        mode=mode,
        allowed_actions=("growth.read_only_analysis",),
        forbidden_actions=(),
        allowed_channels=("internal",),
        allowed_audiences=("business_owner",),
        schedule=AutomationSchedule(timezone_name="Europe/Tallinn"),
        expires_at=(_NOW + timedelta(days=30)).isoformat(),
        stop_conditions=("business_suspended", "owner_stop"),
    )


def _seed():
    owner_user_id = 9_300_004_000 + (uuid.uuid4().int % 900_000)
    with db() as conn:
        tenancy = TenancyRepository(conn)
        access = tenancy.create_business(
            owner_user_id=owner_user_id,
            name=f"Automation policy concurrency {uuid.uuid4().hex[:10]}",
            now=_NOW.isoformat(),
        )
        return tenancy.resolve_context(
            user_id=owner_user_id,
            business_id=access.business.id,
        )


def _run_pair(worker):
    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(worker, (0, 1)))


def _cleanup(business_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM businesses WHERE id=?", (business_id,))


def main() -> int:
    if not CONFIG.uses_postgres:
        raise SystemExit(
            "POSTGRES_AUTOMATION_POLICY_CONCURRENCY_FAILED: postgres is required"
        )
    if not (os.getenv("DATABASE_URL") or "").strip():
        raise SystemExit(
            "POSTGRES_AUTOMATION_POLICY_CONCURRENCY_FAILED: DATABASE_URL is required"
        )
    if (os.getenv("POSTGRES_REUSE_CONNECTIONS") or "").strip() != "0":
        raise SystemExit(
            "POSTGRES_AUTOMATION_POLICY_CONCURRENCY_FAILED: "
            "POSTGRES_REUSE_CONNECTIONS=0 is required"
        )

    init_db()
    actor = _seed()
    try:
        create_gate = Barrier(2)

        def create_draft(index: int) -> str:
            create_gate.wait(timeout=15)
            mode = AutomationMode.AUTOPILOT if index == 0 else AutomationMode.NORMAL
            try:
                with db() as conn:
                    policy = AutomationPolicyRepository(conn).create_draft(
                        actor=actor,
                        spec=_spec(mode),
                        expected_latest_version=0,
                        now=_NOW + timedelta(seconds=index),
                    )
                return f"created:{policy.id}:{policy.policy_hash}"
            except AutomationPolicyConflict as exc:
                return f"conflict:{exc}"

        create_results = _run_pair(create_draft)
        created = [item for item in create_results if item.startswith("created:")]
        conflicts = [item for item in create_results if item.startswith("conflict:")]
        assert len(created) == 1, create_results
        assert len(conflicts) == 1, create_results
        _, policy_id, policy_hash = created[0].split(":", 2)

        approve_gate = Barrier(2)

        def approve(_index: int) -> str:
            approve_gate.wait(timeout=15)
            try:
                with db() as conn:
                    policy = AutomationPolicyRepository(conn).approve(
                        actor=actor,
                        policy_id=policy_id,
                        expected_policy_hash=policy_hash,
                        now=_NOW + timedelta(minutes=1),
                    )
                return f"approved:{policy.id}"
            except AutomationPolicyConflict as exc:
                return f"conflict:{exc}"

        approve_results = _run_pair(approve)
        approvals = [item for item in approve_results if item.startswith("approved:")]
        approval_conflicts = [item for item in approve_results if item.startswith("conflict:")]
        assert len(approvals) == 1, approve_results
        assert len(approval_conflicts) == 1, approve_results

        with db() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS approved,
                       MAX(version) AS max_version
                FROM clientplatform_automation_policies
                WHERE business_id=?
                """,
                (actor.business_id,),
            ).fetchone()
            draft_audit = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM clientplatform_admin_audit_events
                    WHERE business_id=? AND action='automation_policy_draft_created'
                    """,
                    (actor.business_id,),
                ).fetchone()[0]
            )
            approval_audit = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM clientplatform_admin_audit_events
                    WHERE business_id=? AND action='automation_policy_owner_approved'
                    """,
                    (actor.business_id,),
                ).fetchone()[0]
            )

        total = int(row["total"])
        approved_count = int(row["approved"] or 0)
        max_version = int(row["max_version"] or 0)
        assert total == 1, row
        assert approved_count == 1, row
        assert max_version == 1, row
        assert draft_audit == 1, draft_audit
        assert approval_audit == 1, approval_audit

        print(
            json.dumps(
                {
                    "ok": True,
                    "probe": "postgres_automation_policy_concurrency",
                    "create_results": create_results,
                    "approve_results": approve_results,
                    "policy_count": total,
                    "approved_count": approved_count,
                    "max_version": max_version,
                    "draft_audit": draft_audit,
                    "approval_audit": approval_audit,
                },
                sort_keys=True,
            )
        )
        print("POSTGRES_AUTOMATION_POLICY_CONCURRENCY_OK")
        return 0
    finally:
        _cleanup(actor.business_id)


if __name__ == "__main__":
    raise SystemExit(main())
