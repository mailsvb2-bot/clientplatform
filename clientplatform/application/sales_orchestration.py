from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from clientplatform.application.sales_agent import build_next_sales_plan_in_transaction
from clientplatform.domain.commercial_ladder import CommercialOfferCandidate, CommercialStepKind
from clientplatform.domain.sales import SalesActionKind, SalesActionPlan
from clientplatform.domain.sales_handoff import HandoffSignal, evaluate_handoff
from clientplatform.domain.sales_state_machine import (
    SalesConversationEvent,
    SalesConversationState,
    SalesTransition,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.commercial_ladder_repository import (
    CommercialLadderRepository,
)
from clientplatform.infrastructure.sales_action_repository import SalesActionRepository
from clientplatform.infrastructure.sales_handoff_repository import SalesHandoffRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.sales_state_repository import SalesStateRepository
from services.db import get_db


@dataclass(frozen=True, slots=True)
class SalesCommercialCandidateSelection:
    ladder_id: str
    step_id: str
    kind: CommercialStepKind
    title: str
    offering_id: str | None
    requires_human_approval: bool
    evidence_score: float


@dataclass(frozen=True, slots=True)
class SalesOrchestrationResult:
    transition: SalesTransition
    signal_applied: bool
    plan: SalesActionPlan | None
    plan_id: str | None
    handoff: dict[str, object] | None
    commercial_candidate: SalesCommercialCandidateSelection | None


_STAGE_EVIDENCE_SCORE = {
    SalesConversationState.DISCOVERED: 0.10,
    SalesConversationState.ENGAGED: 0.35,
    SalesConversationState.NEED_KNOWN: 0.50,
    SalesConversationState.QUALIFIED: 0.75,
    SalesConversationState.OFFER_PRESENTED: 0.82,
    SalesConversationState.CHECKOUT: 0.92,
    SalesConversationState.WON: 1.00,
    SalesConversationState.LOST: 0.00,
    SalesConversationState.HANDOFF: 0.00,
}


def _bounded_score(value: float) -> float:
    score = float(value)
    if not math.isfinite(score):
        raise ValueError("evidence_score must be finite")
    return max(0.0, min(score, 1.0))


def _row_value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _select_commercial_candidate(
    *,
    conn: Any,
    actor: TenantContext,
    lead_id: str,
    plan_id: str,
    evidence_score: float,
    dedupe_key: str,
) -> SalesCommercialCandidateSelection | None:
    sales = SalesRepository(conn)
    lead = sales.get_lead(actor=actor, lead_id=lead_id)
    ladders = conn.execute(
        """
        SELECT id
        FROM commercial_ladders
        WHERE business_id=? AND status='active'
        ORDER BY created_at, id
        """,
        (lead.business_id,),
    ).fetchall()
    if not ladders:
        return None

    repository = CommercialLadderRepository(conn)
    fallback: tuple[str, CommercialOfferCandidate] | None = None
    selected: tuple[str, CommercialOfferCandidate] | None = None
    for row in ladders:
        ladder_id = str(_row_value(row, "id", 0))
        candidates = repository.candidates(
            actor=actor,
            ladder_id=ladder_id,
            evidence_score=evidence_score,
        )
        for candidate in candidates:
            if fallback is None:
                fallback = (ladder_id, candidate)
            if lead.offering_id is not None and candidate.offering_id == lead.offering_id:
                selected = (ladder_id, candidate)
                break
        if selected is not None:
            break
    selected = selected or fallback
    if selected is None:
        return None

    ladder_id, candidate = selected
    result = SalesCommercialCandidateSelection(
        ladder_id=ladder_id,
        step_id=candidate.step_id,
        kind=candidate.kind,
        title=candidate.title,
        offering_id=candidate.offering_id,
        requires_human_approval=candidate.requires_human_approval,
        evidence_score=evidence_score,
    )
    sales.record_event(
        actor=actor,
        lead_id=lead.id,
        event_type="commercial_candidate_selected",
        dedupe_key=f"commercial-candidate:{dedupe_key}",
        payload={
            "plan_id": plan_id,
            "ladder_id": result.ladder_id,
            "step_id": result.step_id,
            "kind": result.kind.value,
            "title": result.title,
            "offering_id": result.offering_id,
            "requires_human_approval": result.requires_human_approval,
            "evidence_score": result.evidence_score,
        },
    )
    return result


def orchestrate_sales_signal_in_transaction(
    *,
    conn: Any,
    actor: TenantContext,
    lead_id: str,
    event: SalesConversationEvent | str,
    dedupe_key: str,
    model_confidence: float,
    metadata: dict[str, object] | None = None,
    unanswered_inbound: bool = False,
    explicit_human_request: bool = False,
    sensitive_context: bool = False,
    pricing_exception: bool = False,
    negative_sentiment: bool = False,
    failed_attempts: int = 0,
    evidence_score: float | None = None,
) -> SalesOrchestrationResult:
    """Close one replay-safe sales signal into persisted owner work.

    The transaction owns the complete internal chain:
    signal -> state transition -> planner -> ActionPlan -> optional handoff ->
    optional commercial candidate. No external message is sent here.
    """

    transition, applied = SalesStateRepository(conn).apply(
        actor=actor,
        lead_id=lead_id,
        event=event,
        dedupe_key=dedupe_key,
        metadata=metadata,
    )
    if not applied:
        return SalesOrchestrationResult(
            transition=transition,
            signal_applied=False,
            plan=None,
            plan_id=None,
            handoff=None,
            commercial_candidate=None,
        )

    handoff_signal: HandoffSignal | None = evaluate_handoff(
        model_confidence=model_confidence,
        explicit_human_request=explicit_human_request,
        sensitive_context=sensitive_context,
        pricing_exception=pricing_exception,
        negative_sentiment=negative_sentiment,
        failed_attempts=failed_attempts,
    )

    if (
        handoff_signal is not None
        and not explicit_human_request
        and not sensitive_context
        and float(model_confidence) >= 0.72
    ):
        plan = SalesActionPlan(
            lead_id=lead_id,
            action_kind=SalesActionKind.HUMAN_HANDOFF,
            rationale=f"handoff:{handoff_signal.reason.value}",
            requires_approval=False,
        )
        plan_id = SalesRepository(conn).save_plan(actor=actor, plan=plan)
    else:
        plan, plan_id = build_next_sales_plan_in_transaction(
            conn=conn,
            actor=actor,
            lead_id=lead_id,
            model_confidence=model_confidence,
            unanswered_inbound=unanswered_inbound,
            explicit_human_request=explicit_human_request,
            sensitive_context=sensitive_context,
        )

    handoff: dict[str, object] | None = None
    if handoff_signal is not None:
        handoff = SalesHandoffRepository(conn).open(
            actor=actor,
            lead_id=lead_id,
            signal=handoff_signal,
            context={
                "source_event": transition.event.value,
                "state": transition.current.value,
                "plan_id": plan_id,
                "metadata": dict(metadata or {}),
            },
        )

    commercial_candidate: SalesCommercialCandidateSelection | None = None
    if plan.action_kind not in {SalesActionKind.NOOP, SalesActionKind.HUMAN_HANDOFF}:
        score = _bounded_score(
            _STAGE_EVIDENCE_SCORE[transition.current]
            if evidence_score is None
            else evidence_score
        )
        commercial_candidate = _select_commercial_candidate(
            conn=conn,
            actor=actor,
            lead_id=lead_id,
            plan_id=plan_id,
            evidence_score=score,
            dedupe_key=dedupe_key,
        )

    return SalesOrchestrationResult(
        transition=transition,
        signal_applied=True,
        plan=plan,
        plan_id=plan_id,
        handoff=handoff,
        commercial_candidate=commercial_candidate,
    )


def orchestrate_sales_signal(
    *,
    actor: TenantContext,
    lead_id: str,
    event: SalesConversationEvent | str,
    dedupe_key: str,
    model_confidence: float,
    metadata: dict[str, object] | None = None,
    unanswered_inbound: bool = False,
    explicit_human_request: bool = False,
    sensitive_context: bool = False,
    pricing_exception: bool = False,
    negative_sentiment: bool = False,
    failed_attempts: int = 0,
    evidence_score: float | None = None,
) -> SalesOrchestrationResult:
    with get_db() as conn:
        return orchestrate_sales_signal_in_transaction(
            conn=conn,
            actor=actor,
            lead_id=lead_id,
            event=event,
            dedupe_key=dedupe_key,
            model_confidence=model_confidence,
            metadata=metadata,
            unanswered_inbound=unanswered_inbound,
            explicit_human_request=explicit_human_request,
            sensitive_context=sensitive_context,
            pricing_exception=pricing_exception,
            negative_sentiment=negative_sentiment,
            failed_attempts=failed_attempts,
            evidence_score=evidence_score,
        )


def authorize_sales_outbound(
    *, actor: TenantContext, plan_id: str
) -> dict[str, object]:
    """Return a dispatch authorization only after a separate explicit approval."""

    with get_db() as conn:
        return SalesActionRepository(conn).authorize_outbound(
            actor=actor,
            plan_id=plan_id,
        )


def approve_and_authorize_sales_outbound(
    *, actor: TenantContext, plan_id: str
) -> dict[str, object]:
    """Atomically approve a plan and expose its allowed outbound target.

    This is the owner approval gate. It still does not call an external provider;
    a sender must explicitly consume the returned authorization afterwards.
    """

    with get_db() as conn:
        repository = SalesActionRepository(conn)
        repository.approve(actor=actor, plan_id=plan_id)
        return repository.authorize_outbound(actor=actor, plan_id=plan_id)


__all__ = [
    "SalesCommercialCandidateSelection",
    "SalesOrchestrationResult",
    "approve_and_authorize_sales_outbound",
    "authorize_sales_outbound",
    "orchestrate_sales_signal",
    "orchestrate_sales_signal_in_transaction",
]
