from __future__ import annotations

from clientplatform.domain.commercial_ladder import (
    CommercialLadderStep,
    CommercialOfferCandidate,
    CommercialStepKind,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.commercial_ladder_repository import (
    CommercialLadderRepository,
)
from services.db import get_db, get_db_ro



def create_commercial_ladder(
    *, actor: TenantContext, name: str
) -> str:
    with get_db() as conn:
        return CommercialLadderRepository(conn).create_ladder(
            actor=actor, name=name
        )


def add_commercial_ladder_step(
    *,
    actor: TenantContext,
    ladder_id: str,
    position: int,
    kind: CommercialStepKind | str,
    title: str,
    offering_id: str | None = None,
    min_evidence_score: float = 0.0,
    requires_human_approval: bool = True,
) -> CommercialLadderStep:
    with get_db() as conn:
        return CommercialLadderRepository(conn).add_step(
            actor=actor,
            ladder_id=ladder_id,
            position=position,
            kind=kind,
            title=title,
            offering_id=offering_id,
            min_evidence_score=min_evidence_score,
            requires_human_approval=requires_human_approval,
        )

def get_commercial_offer_candidates(
    *,
    actor: TenantContext,
    ladder_id: str,
    evidence_score: float,
    completed_step_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[CommercialOfferCandidate, ...]:
    """Return eligible next offers; never auto-sell or mutate pricing."""

    with get_db_ro() as conn:
        return CommercialLadderRepository(conn).candidates(
            actor=actor,
            ladder_id=ladder_id,
            evidence_score=evidence_score,
            completed_step_ids=completed_step_ids,
        )
