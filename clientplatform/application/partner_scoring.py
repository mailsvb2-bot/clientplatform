from __future__ import annotations

import math

from clientplatform.domain.partners import (
    ContactBasis,
    PartnerCampaignGoal,
    PartnerCandidate,
    PartnerFitScore,
)


def score_partner(candidate: PartnerCandidate, goal: PartnerCampaignGoal) -> PartnerFitScore:
    """Explainable 0..100 ranking; advisory only, never sends anything."""

    goal_terms = {_norm(term) for term in goal.audience_terms if _norm(term)}
    candidate_terms = {
        _norm(term)
        for term in (
            *candidate.tags,
            candidate.audience_summary,
            candidate.recent_topic,
            candidate.name,
        )
        if _norm(term)
    }
    overlap = _semantic_overlap(goal_terms, candidate_terms)
    relevance = min(100.0, 28.0 + overlap * 72.0) if goal_terms else 55.0

    audience_quality = _audience_score(candidate.follower_count)
    contactability = {
        ContactBasis.OPTED_IN: 100.0,
        ContactBasis.EXISTING_RELATIONSHIP: 95.0,
        ContactBasis.PUBLIC_BUSINESS_CONTACT: 82.0,
        ContactBasis.UNKNOWN: 20.0,
        ContactBasis.NONE: 0.0,
    }[candidate.contact_basis]
    if not candidate.contact_value:
        contactability = min(contactability, 10.0)

    collaboration_fit = 50.0
    if candidate.recent_topic:
        collaboration_fit += 18.0
    if candidate.audience_summary:
        collaboration_fit += 12.0
    if candidate.source_url:
        collaboration_fit += 8.0
    collaboration_fit = min(collaboration_fit, 100.0)

    risk_penalty = 0.0
    reasons: list[str] = []
    if candidate.competitor:
        risk_penalty += 65.0
        reasons.append("direct_competitor")
    if not candidate.contact_basis.permits_first_contact:
        risk_penalty += 20.0
        reasons.append("no_first_contact_basis")
    if candidate.follower_count is not None and candidate.follower_count > 1_000_000:
        risk_penalty += 8.0
        reasons.append("very_large_audience_lower_response_probability")

    total = (
        relevance * 0.40
        + audience_quality * 0.20
        + contactability * 0.20
        + collaboration_fit * 0.20
        - risk_penalty
    )
    total = max(0.0, min(100.0, total))
    if overlap > 0.35:
        reasons.append("strong_audience_overlap")
    if candidate.recent_topic:
        reasons.append("recent_topic_available_for_personalization")
    if candidate.contact_basis.permits_first_contact:
        reasons.append("contact_basis_allows_first_contact")

    return PartnerFitScore(
        candidate_id=candidate.id,
        total=total,
        relevance=relevance,
        audience_quality=audience_quality,
        contactability=contactability,
        collaboration_fit=collaboration_fit,
        risk_penalty=min(100.0, risk_penalty),
        reasons=tuple(reasons),
    )


def _audience_score(followers: int | None) -> float:
    if followers is None:
        return 55.0
    if followers <= 0:
        return 20.0
    # Small/mid-size partners are intentionally favoured: they tend to be more
    # reachable while still having meaningful distribution.
    center = math.log10(8_000)
    distance = abs(math.log10(max(1, followers)) - center)
    return max(30.0, min(100.0, 100.0 - distance * 28.0))


def _semantic_overlap(goal_terms: set[str], candidate_terms: set[str]) -> float:
    if not goal_terms or not candidate_terms:
        return 0.0
    hits = 0
    for goal in goal_terms:
        if any(goal in candidate or candidate in goal for candidate in candidate_terms):
            hits += 1
    return hits / max(1, len(goal_terms))


def _norm(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split()).strip()


__all__ = ["score_partner"]
