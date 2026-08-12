from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import date, datetime

from clientplatform.application.creative_growth_analytics import (
    CreativeGrowthOutcomeSnapshot,
    get_creative_growth_outcomes,
)
from clientplatform.application.creative_growth_optimization import (
    apply_creative_growth_recommendation,
)
from clientplatform.application.creative_growth_optimizer import (
    CreativeOptimizationMetric,
    CreativeOptimizationRecommendation,
    recommend_creative_growth_allocation,
)
from clientplatform.domain.creative_growth import CreativeTrafficPlan, CreativeVariantOutcome
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.creative_growth_optimization_repository import (
    StaleCreativeOptimizationError,
)
from clientplatform.infrastructure.creative_growth_repository import CreativeGrowthRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db_ro


class CreativeWinnerApplyError(RuntimeError):
    """A reviewed optimization can no longer be applied safely."""


@dataclass(frozen=True, slots=True)
class CreativeWinnerPreview:
    plan: CreativeTrafficPlan
    variants: tuple[CreativeVariantOutcome, ...]
    recommendation: CreativeOptimizationRecommendation
    date_from: str
    date_to: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class CreativeWinnerApplyResult:
    preview: CreativeWinnerPreview
    updated_plan: CreativeTrafficPlan


def _fingerprint(
    *,
    snapshot: CreativeGrowthOutcomeSnapshot,
    recommendation: CreativeOptimizationRecommendation,
    min_leads_per_arm: int,
    exploration_floor_bps: int,
    max_shift_bps: int,
) -> str:
    payload = {
        "trial_id": recommendation.trial_id,
        "revision": recommendation.trial_revision,
        "date_from": snapshot.date_from,
        "date_to": snapshot.date_to,
        "metric": recommendation.metric.value,
        "status": recommendation.status.value,
        "reason": recommendation.reason,
        "winner_variant_id": recommendation.winner_variant_id,
        "variants": [
            {
                "variant_id": item.variant_id,
                "publication_job_id": item.publication_job_id,
                "scope": item.attribution_scope.value,
                "leads": item.leads,
                "bookings": item.bookings,
                "won": item.won,
            }
            for item in snapshot.variants
        ],
        "evidence": [
            {
                "variant_id": item.variant_id,
                "publication_job_id": item.publication_job_id,
                "leads": item.leads,
                "successes": item.successes,
                "current_allocation_bps": item.current_allocation_bps,
                "proposed_allocation_bps": item.proposed_allocation_bps,
            }
            for item in recommendation.evidence
        ],
        "policy": {
            "min_leads_per_arm": int(min_leads_per_arm),
            "exploration_floor_bps": int(exploration_floor_bps),
            "max_shift_bps": int(max_shift_bps),
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def list_creative_trials(*, actor: TenantContext) -> tuple[CreativeTrafficPlan, ...]:
    with get_db_ro() as conn:
        return CreativeGrowthRepository(conn).list(actor=actor)


def resolve_creative_trial_actor(*, user_id: int, trial_id: str) -> TenantContext:
    """Resolve trial ownership and then enforce tenant membership on one connection."""

    normalized_trial = normalize_uuid(trial_id, field_name="trial_id")
    with get_db_ro() as conn:
        row = conn.execute(
            "SELECT business_id FROM creative_growth_trials WHERE id=? LIMIT 1",
            (normalized_trial,),
        ).fetchone()
        if row is None:
            raise LookupError("creative growth trial was not found")
        business_id = str(row["business_id"] if hasattr(row, "keys") else row[0])
        return TenancyRepository(conn).resolve_context(
            user_id=int(user_id),
            business_id=business_id,
        )


def preview_creative_winner(
    *,
    actor: TenantContext,
    trial_id: str,
    days: int = 30,
    now: datetime | date | None = None,
    metric: CreativeOptimizationMetric = CreativeOptimizationMetric.BOOKINGS,
    min_leads_per_arm: int = 30,
    exploration_floor_bps: int = 1_000,
    max_shift_bps: int = 1_000,
) -> CreativeWinnerPreview:
    snapshot = get_creative_growth_outcomes(
        actor=actor,
        trial_id=trial_id,
        days=days,
        now=now,
    )
    recommendation = recommend_creative_growth_allocation(
        snapshot,
        metric=metric,
        min_leads_per_arm=min_leads_per_arm,
        exploration_floor_bps=exploration_floor_bps,
        max_shift_bps=max_shift_bps,
    )
    return CreativeWinnerPreview(
        plan=snapshot.plan,
        variants=snapshot.variants,
        recommendation=recommendation,
        date_from=snapshot.date_from,
        date_to=snapshot.date_to,
        fingerprint=_fingerprint(
            snapshot=snapshot,
            recommendation=recommendation,
            min_leads_per_arm=min_leads_per_arm,
            exploration_floor_bps=exploration_floor_bps,
            max_shift_bps=max_shift_bps,
        ),
    )


def apply_creative_winner(
    *,
    actor: TenantContext,
    trial_id: str,
    expected_revision: int,
    expected_fingerprint: str,
    confirmed: bool,
    days: int = 30,
    now: datetime | date | None = None,
    metric: CreativeOptimizationMetric = CreativeOptimizationMetric.BOOKINGS,
    min_leads_per_arm: int = 30,
    exploration_floor_bps: int = 1_000,
    max_shift_bps: int = 1_000,
) -> CreativeWinnerApplyResult:
    """Recompute evidence, verify preview identity, then delegate to CAS apply."""

    if confirmed is not True:
        raise CreativeWinnerApplyError("creative winner requires explicit confirmation")
    preview = preview_creative_winner(
        actor=actor,
        trial_id=trial_id,
        days=days,
        now=now,
        metric=metric,
        min_leads_per_arm=min_leads_per_arm,
        exploration_floor_bps=exploration_floor_bps,
        max_shift_bps=max_shift_bps,
    )
    recommendation = preview.recommendation
    if int(expected_revision) != recommendation.trial_revision:
        raise CreativeWinnerApplyError("creative winner recommendation is stale")
    if not hmac.compare_digest(
        str(expected_fingerprint or ""),
        preview.fingerprint,
    ):
        raise CreativeWinnerApplyError("creative winner evidence changed")
    if not recommendation.can_apply:
        raise CreativeWinnerApplyError("creative winner recommendation is not actionable")
    try:
        updated = apply_creative_growth_recommendation(
            actor=actor,
            recommendation=recommendation,
            confirmed=True,
        )
    except StaleCreativeOptimizationError as exc:
        raise CreativeWinnerApplyError("creative winner recommendation is stale") from exc
    return CreativeWinnerApplyResult(preview=preview, updated_plan=updated)


__all__ = [
    "CreativeWinnerApplyError",
    "CreativeWinnerApplyResult",
    "CreativeWinnerPreview",
    "apply_creative_winner",
    "list_creative_trials",
    "preview_creative_winner",
    "resolve_creative_trial_actor",
]
