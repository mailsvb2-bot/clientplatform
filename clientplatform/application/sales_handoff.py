from __future__ import annotations

from clientplatform.domain.sales_handoff import HandoffSignal, evaluate_handoff
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.sales_handoff_repository import SalesHandoffRepository
from services.db import get_db, get_db_ro


def open_sales_handoff(
    *,
    actor: TenantContext,
    lead_id: str,
    signal: HandoffSignal,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    with get_db() as conn:
        return SalesHandoffRepository(conn).open(
            actor=actor,
            lead_id=lead_id,
            signal=signal,
            context=context,
        )


def list_sales_handoffs(*, actor: TenantContext) -> list[dict[str, object]]:
    with get_db_ro() as conn:
        return SalesHandoffRepository(conn).list_open(actor=actor)


def claim_sales_handoff(
    *, actor: TenantContext, handoff_id: str
) -> dict[str, object]:
    with get_db() as conn:
        return SalesHandoffRepository(conn).claim(
            actor=actor,
            handoff_id=handoff_id,
        )


def resolve_sales_handoff(
    *, actor: TenantContext, handoff_id: str
) -> dict[str, object]:
    with get_db() as conn:
        return SalesHandoffRepository(conn).resolve(
            actor=actor,
            handoff_id=handoff_id,
        )

def evaluate_and_open_sales_handoff(
    *,
    actor: TenantContext,
    lead_id: str,
    model_confidence: float,
    explicit_human_request: bool = False,
    sensitive_context: bool = False,
    pricing_exception: bool = False,
    negative_sentiment: bool = False,
    failed_attempts: int = 0,
    context: dict[str, object] | None = None,
) -> dict[str, object] | None:
    signal = evaluate_handoff(
        model_confidence=model_confidence,
        explicit_human_request=explicit_human_request,
        sensitive_context=sensitive_context,
        pricing_exception=pricing_exception,
        negative_sentiment=negative_sentiment,
        failed_attempts=failed_attempts,
    )
    if signal is None:
        return None
    return open_sales_handoff(
        actor=actor, lead_id=lead_id, signal=signal, context=context
    )
