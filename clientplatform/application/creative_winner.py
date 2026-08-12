from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime

from clientplatform.application.creative_growth_analytics import (
    CreativeGrowthOutcomeSnapshot,
    get_creative_growth_outcomes,
)
from clientplatform.domain.creative_growth import CreativeTrafficPlan
from clientplatform.domain.creative_winner_policy import (
    CreativeWinnerPolicy,
    CreativeWinnerRecommendation,
    recommend_creative_winner,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.creative_growth_repository import CreativeGrowthRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db, get_db_ro


class CreativeWinnerApplyError(RuntimeError):
    """A winner recommendation cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class CreativeWinnerPreview:
    recommendation: CreativeWinnerRecommendation
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
    recommendation: CreativeWinnerRecommendation,
    policy: CreativeWinnerPolicy,
) -> str:
    payload = {
        "trial_id": recommendation.trial_id,
        "revision": recommendation.expected_revision,
        "date_from": snapshot.date_from,
        "date_to": snapshot.date_to,
        "decision": recommendation.decision.value,
        "reason": recommendation.reason,
        "metric": None if recommendation.metric is None else recommendation.metric.value,
        "winner_variant_id": recommendation.winner_variant_id,
        "evidence": [
            {
                "variant_id": item.variant_id,
                "leads": item.leads,
                "successes": item.successes,
            }
            for item in recommendation.evidence
        ],
        "allocations": list(recommendation.recommended_allocations),
        "policy": asdict(policy),
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
    """Resolve a trial to an actor without exposing it before membership checks."""

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
    policy: CreativeWinnerPolicy | None = None,
) -> CreativeWinnerPreview:
    rules = policy or CreativeWinnerPolicy()
    snapshot = get_creative_growth_outcomes(
        actor=actor,
        trial_id=trial_id,
        days=days,
        now=now,
    )
    recommendation = recommend_creative_winner(
        plan=snapshot.plan,
        outcomes=snapshot.variants,
        policy=rules,
    )
    return CreativeWinnerPreview(
        recommendation=recommendation,
        date_from=snapshot.date_from,
        date_to=snapshot.date_to,
        fingerprint=_fingerprint(
            snapshot=snapshot,
            recommendation=recommendation,
            policy=rules,
        ),
    )


def apply_creative_winner(
    *,
    actor: TenantContext,
    trial_id: str,
    expected_revision: int,
    expected_fingerprint: str,
    days: int = 30,
    now: datetime | date | None = None,
    policy: CreativeWinnerPolicy | None = None,
) -> CreativeWinnerApplyResult:
    """Recompute evidence, verify preview identity, then CAS the allocation."""

    preview = preview_creative_winner(
        actor=actor,
        trial_id=trial_id,
        days=days,
        now=now,
        policy=policy,
    )
    recommendation = preview.recommendation
    if int(expected_revision) != recommendation.expected_revision:
        raise CreativeWinnerApplyError("creative winner recommendation is stale")
    if not hmac.compare_digest(
        str(expected_fingerprint or ""),
        preview.fingerprint,
    ):
        raise CreativeWinnerApplyError("creative winner evidence changed")
    if not recommendation.can_apply:
        raise CreativeWinnerApplyError("creative winner recommendation is not actionable")

    with get_db() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        try:
            updated = CreativeGrowthRepository(conn).replace_allocations(
                actor=current,
                trial_id=recommendation.trial_id,
                allocations=recommendation.recommended_allocations,
                expected_revision=recommendation.expected_revision,
            )
        except ValueError as exc:
            if "stale" in str(exc).lower():
                raise CreativeWinnerApplyError(
                    "creative winner recommendation is stale"
                ) from exc
            raise
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
