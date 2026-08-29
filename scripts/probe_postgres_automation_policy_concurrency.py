from __future__ import annotations

import hashlib
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
    AutomationApprovalConflict,
    AutomationCandidateAction,
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

        candidate = AutomationCandidateAction(
            business_id=actor.business_id,
            action="sales.followup",
            external_write=True,
            channel="email",
            audience="prospect_opted_in",
            scheduled_at=_NOW + timedelta(minutes=2),
            subject_ref="customer:postgres-probe",
            payload_digest=hashlib.sha256(b"m5002 postgres followup payload").hexdigest(),
        )

        # Promote a policy that explicitly permits this external action but requires
        # owner approval. This remains an authorization-only probe: no provider call.
        with db() as conn:
            repository = AutomationPolicyRepository(conn)
            latest = repository.latest(actor=actor)
            action_spec = AutomationPolicySpec(
                mode=AutomationMode.NORMAL,
                allowed_actions=("growth.read_only_analysis", "sales.followup"),
                forbidden_actions=(),
                allowed_channels=("internal", "email"),
                allowed_audiences=("business_owner", "prospect_opted_in"),
                schedule=AutomationSchedule(timezone_name="Europe/Tallinn"),
                expires_at=(_NOW + timedelta(days=30)).isoformat(),
                approval_required_actions=("sales.followup",),
                approval_required_channels=("email",),
                stop_conditions=("business_suspended", "owner_stop"),
            )
            draft = repository.create_draft(
                actor=actor,
                spec=action_spec,
                expected_latest_version=latest.version if latest else None,
                now=_NOW + timedelta(minutes=2),
            )
            repository.approve(
                actor=actor,
                policy_id=draft.id,
                expected_policy_hash=draft.policy_hash,
                now=_NOW + timedelta(minutes=2),
            )

        request_gate = Barrier(2)

        def request_action(_index: int) -> str:
            request_gate.wait(timeout=15)
            try:
                with db() as conn:
                    approval = AutomationPolicyRepository(conn).request_action_approval(
                        actor=actor,
                        candidate=candidate,
                        idempotency_key="m5002:postgres:exact-replay",
                        now=_NOW + timedelta(minutes=3),
                    )
                return f"requested:{approval.id}:{approval.request_fingerprint}"
            except AutomationApprovalConflict as exc:
                return f"conflict:{exc}"

        request_results = _run_pair(request_action)
        requested = [item for item in request_results if item.startswith("requested:")]
        assert len(requested) == 2, request_results
        approval_ids = {item.split(":", 2)[1] for item in requested}
        assert len(approval_ids) == 1, request_results
        approval_id = next(iter(approval_ids))
        with db() as conn:
            approval = AutomationPolicyRepository(conn).get_action_approval(
                actor=actor,
                approval_id=approval_id,
            )
        request_fingerprint = approval.request_fingerprint

        decision_gate = Barrier(2)

        def decide(index: int) -> str:
            decision_gate.wait(timeout=15)
            try:
                with db() as conn:
                    repository = AutomationPolicyRepository(conn)
                    if index == 0:
                        result = repository.approve_action_approval(
                            actor=actor,
                            approval_id=approval_id,
                            expected_request_fingerprint=request_fingerprint,
                            now=_NOW + timedelta(minutes=4),
                        )
                    else:
                        result = repository.reject_action_approval(
                            actor=actor,
                            approval_id=approval_id,
                            expected_request_fingerprint=request_fingerprint,
                            now=_NOW + timedelta(minutes=4),
                        )
                return f"decided:{result.status.value}"
            except AutomationApprovalConflict as exc:
                return f"conflict:{exc}"

        decision_results = _run_pair(decide)
        decisions = [item for item in decision_results if item.startswith("decided:")]
        decision_conflicts = [item for item in decision_results if item.startswith("conflict:")]
        assert len(decisions) == 1, decision_results
        assert len(decision_conflicts) == 1, decision_results

        with db() as conn:
            final_approval = AutomationPolicyRepository(conn).get_action_approval(
                actor=actor,
                approval_id=approval_id,
            )
            approval_row_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM clientplatform_automation_action_approvals "
                    "WHERE business_id=? AND idempotency_key=?",
                    (actor.business_id, "m5002:postgres:exact-replay"),
                ).fetchone()[0]
            )
            request_audit = int(
                conn.execute(
                    "SELECT COUNT(*) FROM clientplatform_admin_audit_events "
                    "WHERE business_id=? AND action='automation_action_approval_requested'",
                    (actor.business_id,),
                ).fetchone()[0]
            )
            decision_audit = int(
                conn.execute(
                    "SELECT COUNT(*) FROM clientplatform_admin_audit_events "
                    "WHERE business_id=? AND action IN "
                    "('automation_action_owner_approved','automation_action_owner_rejected')",
                    (actor.business_id,),
                ).fetchone()[0]
            )
        assert approval_row_count == 1, approval_row_count
        assert request_audit == 1, request_audit
        assert decision_audit == 1, decision_audit
        assert final_approval.status.value in {"approved", "rejected"}, final_approval

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
        assert total == 2, row
        assert approved_count == 1, row
        assert max_version == 2, row
        assert draft_audit == 2, draft_audit
        assert approval_audit == 2, approval_audit

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
                    "action_request_results": request_results,
                    "action_decision_results": decision_results,
                    "action_approval_status": final_approval.status.value,
                    "action_approval_row_count": approval_row_count,
                    "action_request_audit": request_audit,
                    "action_decision_audit": decision_audit,
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
