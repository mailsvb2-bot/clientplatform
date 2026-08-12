from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Generic, TypeVar

from clientplatform.application.sales_ai_settings import business_sales_ai_consent_in_conn
from clientplatform.application.sales_orchestration import (
    SalesOrchestrationResult,
    orchestrate_sales_signal_in_transaction,
)
from clientplatform.domain.sales import SalesActionKind, SalesLead
from clientplatform.domain.sales_ai_jobs import SalesAIJob, SalesAIJobLeaseLost, SalesAIJobStatus
from clientplatform.domain.sales_ai_policy import prepare_sales_ai_text, validated_sales_ai_milestones
from clientplatform.domain.sales_intelligence import (
    SalesAIAnalysis,
    SalesAIOfferKind,
    SalesAIReplyGoal,
    SalesAIVerifiedOffer,
)
from clientplatform.domain.sales_state_machine import SalesConversationEvent
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.sales_ai_analysis_repository import SalesAIAnalysisRepository
from clientplatform.infrastructure.sales_ai_consent_repository import SalesAIConsentRepository
from clientplatform.infrastructure.sales_ai_job_repository import SalesAIJobRepository
from clientplatform.infrastructure.sales_repository import SalesRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.runtime.sales_ai_config import normalize_sales_ai_provider
from services.db import get_db, get_db_ro


@dataclass(frozen=True, slots=True)
class SalesAIWorkInput:
    job: SalesAIJob
    customer_text: str
    current_stage: str
    source_kind: str
    consent_epoch: int
    text_was_redacted: bool


def _row_value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _owner_actor(conn: Any, *, business_id: str) -> TenantContext:
    row = conn.execute(
        """
        SELECT user_id FROM business_members
        WHERE business_id=? AND role='owner' AND status='active'
        ORDER BY created_at, id LIMIT 1
        """,
        (business_id,),
    ).fetchone()
    if row is None:
        raise ValueError("active business owner is required for sales AI orchestration")
    return TenancyRepository(conn).resolve_context(
        user_id=int(_row_value(row, "user_id", 0)),
        business_id=business_id,
    )


def canonical_sales_ai_parameters(analysis: SalesAIAnalysis) -> dict[str, object]:
    """Map observations only to parameters already owned by canonical #120 logic."""
    return {
        "model_confidence": analysis.confidence,
        "unanswered_inbound": analysis.reply_goal
        in {
            SalesAIReplyGoal.ANSWER_QUESTION,
            SalesAIReplyGoal.RESOLVE_ISSUE,
            SalesAIReplyGoal.PRESENT_OPTION,
            SalesAIReplyGoal.HELP_CHECKOUT,
        },
        "explicit_human_request": analysis.explicit_human_request,
        "sensitive_context": analysis.sensitive_context,
        "pricing_exception": analysis.pricing_exception,
        "negative_sentiment": analysis.negative_sentiment,
        "evidence_score": analysis.purchase_readiness,
    }


def claim_sales_ai_jobs(*, limit: int, lock_ttl_seconds: int) -> list[SalesAIJob]:
    # v3 intentionally allows one claimed network job per worker at a time. This
    # prevents later jobs in a sequential batch from expiring before they start.
    if limit != 1:
        raise ValueError("Sales AI worker claims exactly one job per tick")
    with get_db() as conn:
        return SalesAIJobRepository(conn).claim_due(limit=1, lock_ttl_seconds=lock_ttl_seconds)


_T = TypeVar("_T")


class _LockedDBEgress(Generic[_T]):
    """Own one dedicated DB transaction in a dedicated thread during egress.

    Production PostgreSQL connections are reusable per worker thread. Keeping the
    lock-holder on its own thread avoids interleaving unrelated event-loop DB work
    into the same long-lived transaction while still providing a cross-process row
    lock on the consent/job rows.
    """

    def __init__(self, prepare: Callable[[Any], _T]) -> None:
        self._prepare = prepare
        self._release = threading.Event()
        self._ready: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._value: _T | None = None
        self._error: BaseException | None = None

    def _run(self) -> None:
        assert self._loop is not None and self._ready is not None
        try:
            with get_db() as conn:
                self._value = self._prepare(conn)
                self._loop.call_soon_threadsafe(self._ready.set)
                self._release.wait()
        except BaseException as exc:  # validator: allow-wide-except
            self._error = exc
            self._loop.call_soon_threadsafe(self._ready.set)

    async def __aenter__(self) -> _T:
        self._loop = asyncio.get_running_loop()
        self._ready = asyncio.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="clientplatform-sales-ai-egress-db-lock",
            daemon=True,
        )
        self._thread.start()
        await self._ready.wait()
        if self._error is not None:
            self._release.set()
            await asyncio.to_thread(self._thread.join, 5.0)
            raise self._error
        if self._value is None:
            raise RuntimeError("sales AI egress lock did not produce a value")
        return self._value

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        self._release.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 5.0)
            if self._thread.is_alive():
                raise RuntimeError("sales AI egress DB lock thread did not exit")
        return False


def _prepare_analysis_egress(
    conn: Any,
    *,
    job: SalesAIJob,
    consent_target: str,
) -> SalesAIWorkInput:
    jobs = SalesAIJobRepository(conn)
    current = jobs.get(job_id=job.id, business_id=job.business_id)
    if current.status != SalesAIJobStatus.PROCESSING or current.lock_token != job.lock_token:
        raise SalesAIJobLeaseLost("sales AI job lease changed before egress")
    # Lock the exact job lease and consent row for the entire provider call.
    current = jobs.lock_processing_lease(current)
    consent = SalesAIConsentRepository(conn).lock_valid_consent(
        business_id=job.business_id,
        consent_target=consent_target,
    )
    actor = _owner_actor(conn, business_id=job.business_id)
    lead = SalesRepository(conn).get_lead(actor=actor, lead_id=job.lead_id)
    prepared = prepare_sales_ai_text(jobs.load_customer_message(current), mode=consent.data_mode)
    return SalesAIWorkInput(
        job=current,
        customer_text=prepared.text,
        current_stage=lead.stage.value,
        source_kind=lead.source_kind,
        consent_epoch=consent.consent_epoch,
        text_was_redacted=prepared.redacted,
    )


@asynccontextmanager
async def sales_ai_analysis_egress_permit(
    job: SalesAIJob,
    *,
    consent_target: str,
) -> AsyncIterator[SalesAIWorkInput]:
    """Atomically gate one provider request against tenant consent and job lease.

    A dedicated lock-holder thread owns the DB transaction across the network
    request. Disabling AI or changing the provider must update the same consent row
    and therefore cannot return until an already-started egress completes; all later
    egress sees the new epoch/target and fails closed.
    """
    if job.status != SalesAIJobStatus.PROCESSING or not job.lock_token:
        raise ValueError("sales AI egress requires a processing lease")
    holder = _LockedDBEgress(
        lambda conn: _prepare_analysis_egress(
            conn,
            job=job,
            consent_target=consent_target,
        )
    )
    async with holder as work:
        yield work


def _resolve_verified_offer(
    conn: Any,
    *,
    business_id: str,
    analysis: SalesAIAnalysis,
) -> SalesAIVerifiedOffer | None:
    if analysis.recommended_offer_kind == SalesAIOfferKind.NONE:
        return None
    row = conn.execute(
        """
        SELECT s.title, s.offering_id, p.amount_minor, p.currency
        FROM commercial_ladders l
        JOIN commercial_ladder_steps s
          ON s.business_id=l.business_id AND s.ladder_id=l.id
        LEFT JOIN business_offering_prices p
          ON p.business_id=s.business_id AND p.offering_id=s.offering_id AND p.status='active'
        WHERE l.business_id=? AND l.status='active' AND s.kind=?
          AND s.min_evidence_score<=?
        ORDER BY s.position, s.id
        LIMIT 1
        """,
        (business_id, analysis.recommended_offer_kind.value, analysis.purchase_readiness),
    ).fetchone()
    if row is None:
        return None
    offering_id = _row_value(row, "offering_id", 1)
    amount = _row_value(row, "amount_minor", 2)
    currency = _row_value(row, "currency", 3)
    return SalesAIVerifiedOffer(
        title=str(_row_value(row, "title", 0)),
        offering_id=None if offering_id is None else str(offering_id),
        amount_minor=None if amount is None else int(amount),
        currency=None if currency is None else str(currency),
    )


def _candidate_payload(result: SalesOrchestrationResult) -> dict[str, object] | None:
    candidate = result.commercial_candidate
    if candidate is None:
        return None
    return {
        "ladder_id": candidate.ladder_id,
        "step_id": candidate.step_id,
        "kind": candidate.kind.value,
        "title": candidate.title,
        "offering_id": candidate.offering_id,
        "requires_human_approval": candidate.requires_human_approval,
        "evidence_score": candidate.evidence_score,
    }


def apply_sales_ai_analysis(*, work: SalesAIWorkInput, analysis: SalesAIAnalysis, provider: str, model: str, consent_target: str) -> dict[str, object]:
    provider_name = normalize_sales_ai_provider(provider)
    model_name = str(model or "").strip()
    if not model_name or len(model_name) > 120:
        raise ValueError("sales AI model evidence must be 1..120 characters")

    with get_db() as conn:
        jobs = SalesAIJobRepository(conn)
        current_job = jobs.get(job_id=work.job.id, business_id=work.job.business_id)
        if current_job.status != SalesAIJobStatus.PROCESSING or current_job.lock_token != work.job.lock_token:
            raise SalesAIJobLeaseLost("sales AI job lease changed before result application")
        consent = business_sales_ai_consent_in_conn(conn, business_id=work.job.business_id)
        if (
            consent is None
            or not consent.enabled
            or consent.consent_target != consent_target
            or consent.consent_epoch != work.consent_epoch
        ):
            jobs.cancel(current_job, reason="consent_changed_after_provider_call")
            return {"lead_id": work.job.lead_id, "stale": True, "consent_changed": True}

        actor = _owner_actor(conn, business_id=work.job.business_id)
        sales = SalesRepository(conn)
        lead = sales.get_lead(actor=actor, lead_id=work.job.lead_id)
        if not jobs.lock_if_latest_source(current_job):
            jobs.cancel(current_job, reason="stale_after_provider_call")
            return {"lead_id": lead.id, "stale": True, "consent_changed": False}

        milestones = [event.value for event in validated_sales_ai_milestones(analysis)]
        result = orchestrate_sales_signal_in_transaction(
            conn=conn,
            actor=actor,
            lead_id=lead.id,
            event=SalesConversationEvent.CONTACT_RECORDED,
            dedupe_key=f"sales-ai:{work.job.id}",
            metadata={
                "source": "sales_ai",
                "intent": analysis.intent.value,
                "reply_goal": analysis.reply_goal.value,
                "advisory_milestones": milestones,
                "source_event_dedupe_key": work.job.source_event_dedupe_key,
                "source_order_key": work.job.source_order_key,
                "text_was_redacted": work.text_was_redacted,
            },
            **canonical_sales_ai_parameters(analysis),
        )
        candidate_payload = _candidate_payload(result)
        verified_offer = _resolve_verified_offer(
            conn,
            business_id=work.job.business_id,
            analysis=analysis,
        )
        action_kind = None if result.plan is None else result.plan.action_kind.value
        handoff_id = None if result.handoff is None else str(result.handoff["id"])

        payload = {
            "analysis": analysis.to_event_payload(),
            "provider": provider_name,
            "model": model_name,
            "source_event_dedupe_key": work.job.source_event_dedupe_key,
            "source_order_key": work.job.source_order_key,
            "plan_id": result.plan_id,
            "action_kind": action_kind,
            "commercial_candidate": candidate_payload,
            "verified_offer": None if verified_offer is None else verified_offer.to_payload(),
            "advisory_milestones": milestones,
            "text_was_redacted": work.text_was_redacted,
        }
        sales.record_event(
            actor=actor,
            lead_id=lead.id,
            event_type="ai_sales_analysis",
            dedupe_key=f"ai-analysis:{work.job.id}",
            payload=payload,
        )
        SalesAIAnalysisRepository(conn).upsert_latest(
            business_id=work.job.business_id,
            lead_id=lead.id,
            source_order_key=work.job.source_order_key,
            source_event_dedupe_key=work.job.source_event_dedupe_key,
            analysis=analysis.to_event_payload(),
            provider=provider_name,
            model=model_name,
            plan_id=result.plan_id,
            action_kind=action_kind,
            verified_offer=None if verified_offer is None else verified_offer.to_payload(),
        )
        jobs.mark_done(current_job)
        return {
            "lead_id": lead.id,
            "action_kind": action_kind,
            "plan_id": result.plan_id,
            "handoff_id": handoff_id,
            "commercial_candidate": candidate_payload,
            "verified_offer": None if verified_offer is None else verified_offer.to_payload(),
            "stale": False,
            "consent_changed": False,
        }


def cancel_sales_ai_job(job: SalesAIJob, *, reason: str) -> SalesAIJob:
    with get_db() as conn:
        current = SalesAIJobRepository(conn).get(job_id=job.id, business_id=job.business_id)
        if current.status != SalesAIJobStatus.PROCESSING or current.lock_token != job.lock_token:
            raise SalesAIJobLeaseLost("sales AI job lease changed before cancellation")
        return SalesAIJobRepository(conn).cancel(current, reason=reason)


def retry_sales_ai_job(job: SalesAIJob, *, error_code: str, max_attempts: int) -> SalesAIJob:
    with get_db() as conn:
        return SalesAIJobRepository(conn).retry_or_dead(job, error_code=error_code, max_attempts=max_attempts)


def purge_sales_ai_retention(*, raw_message_ttl_hours: int, analysis_ttl_days: int) -> dict[str, int]:
    with get_db() as conn:
        raw = SalesAIJobRepository(conn).purge_expired_raw_messages(raw_message_ttl_hours=raw_message_ttl_hours)
        analyses = SalesAIAnalysisRepository(conn).purge_expired(analysis_ttl_days=analysis_ttl_days)
        return {"raw_messages_redacted": raw, "analysis_projections_deleted": analyses}


def _load_latest_evidence_in_conn(conn: Any, *, actor: TenantContext, lead_id: str) -> tuple[SalesLead, str, SalesAIAnalysis, str, SalesAIVerifiedOffer | None]:
    current = TenancyRepository(conn).resolve_context(user_id=actor.user_id, business_id=actor.business_id)
    current.assert_can_view_customer_records()
    lead = SalesRepository(conn).get_lead(actor=current, lead_id=lead_id)
    projection = SalesAIAnalysisRepository(conn).get_latest(business_id=current.business_id, lead_id=lead.id)
    if projection is None:
        raise ValueError("sales AI evidence is not ready for this lead")
    analysis = SalesAIAnalysis.from_mapping(projection["analysis"])
    source_event_key = str(projection["source_event_dedupe_key"])
    source_order_key = str(projection["source_order_key"])
    plan_id = str(projection.get("plan_id") or "")
    action_kind = str(projection.get("action_kind") or "")
    latest_order = SalesAIJobRepository(conn).latest_source_order(business_id=current.business_id, lead_id=lead.id)
    if latest_order is not None and latest_order != source_order_key:
        raise ValueError("sales AI analysis for the newest customer message is still pending")
    if not plan_id or not action_kind:
        raise ValueError("sales AI evidence has no current outbound plan")
    plan_row = conn.execute(
        "SELECT action_kind, status FROM clientplatform_sales_action_plans WHERE id=? AND business_id=? AND lead_id=? LIMIT 1",
        (plan_id, current.business_id, lead.id),
    ).fetchone()
    if plan_row is None or str(_row_value(plan_row, "action_kind", 0)) != action_kind or str(_row_value(plan_row, "status", 1)) not in {"planned", "approved"}:
        raise ValueError("sales AI canonical action plan is stale")
    if action_kind in {SalesActionKind.HUMAN_HANDOFF.value, SalesActionKind.NOOP.value}:
        raise ValueError("sales AI item requires human handling instead of a draft")
    message_row = conn.execute(
        "SELECT payload_json FROM clientplatform_sales_events WHERE business_id=? AND lead_id=? AND event_type='customer_message' AND dedupe_key=? LIMIT 1",
        (current.business_id, lead.id, source_event_key),
    ).fetchone()
    if message_row is None:
        raise ValueError("sales AI source customer message is unavailable")
    try:
        message_payload = json.loads(str(_row_value(message_row, "payload_json", 0) or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("stored customer message is invalid") from exc
    message_text = str((message_payload or {}).get("text") or "").strip()
    if not message_text:
        raise ValueError("sales AI source customer message expired or is empty")
    offer_payload = projection.get("verified_offer")
    offer = None if offer_payload is None else SalesAIVerifiedOffer.from_mapping(offer_payload)
    return lead, message_text, analysis, action_kind, offer


def load_latest_sales_ai_evidence(*, actor: TenantContext, lead_id: str) -> tuple[SalesLead, str, SalesAIAnalysis, str, SalesAIVerifiedOffer | None]:
    with get_db_ro() as conn:
        return _load_latest_evidence_in_conn(conn, actor=actor, lead_id=lead_id)


def _prepare_draft_egress(
    conn: Any,
    *,
    actor: TenantContext,
    lead_id: str,
    consent_target: str,
) -> tuple[SalesLead, str, SalesAIAnalysis, str, SalesAIVerifiedOffer | None]:
    current = TenancyRepository(conn).resolve_context(
        user_id=actor.user_id,
        business_id=actor.business_id,
    )
    current.assert_can_view_customer_records()
    consent = SalesAIConsentRepository(conn).lock_valid_consent(
        business_id=current.business_id,
        consent_target=consent_target,
    )
    lead, text, analysis, action, offer = _load_latest_evidence_in_conn(
        conn,
        actor=current,
        lead_id=lead_id,
    )
    prepared = prepare_sales_ai_text(text, mode=consent.data_mode)
    return lead, prepared.text, analysis, action, offer


@asynccontextmanager
async def sales_ai_draft_egress_permit(
    *,
    actor: TenantContext,
    lead_id: str,
    consent_target: str,
) -> AsyncIterator[tuple[SalesLead, str, SalesAIAnalysis, str, SalesAIVerifiedOffer | None]]:
    holder = _LockedDBEgress(
        lambda conn: _prepare_draft_egress(
            conn,
            actor=actor,
            lead_id=lead_id,
            consent_target=consent_target,
        )
    )
    async with holder as evidence:
        yield evidence


__all__ = [
    "SalesAIWorkInput",
    "apply_sales_ai_analysis",
    "cancel_sales_ai_job",
    "canonical_sales_ai_parameters",
    "claim_sales_ai_jobs",
    "load_latest_sales_ai_evidence",
    "purge_sales_ai_retention",
    "retry_sales_ai_job",
    "sales_ai_analysis_egress_permit",
    "sales_ai_draft_egress_permit",
]
