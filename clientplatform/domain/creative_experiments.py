from __future__ import annotations

import hashlib
import math
from statistics import NormalDist
from dataclasses import dataclass

_OBJECTIVES = frozenset({"ctr", "lead_rate", "booking_rate", "won_rate", "cost_per_booking"})
_RATE_OBJECTIVES = frozenset({"ctr", "lead_rate", "booking_rate", "won_rate"})


def stable_experiment_id(
    *,
    business_id: str,
    publication_job_id: str,
    kind: str,
    country_code: str = "",
    creative_fingerprint: str = "",
) -> str:
    parts = [str(business_id or "").strip(), str(publication_job_id or "").strip(), str(kind or "").strip().lower()]
    if not all(parts) or parts[2] not in {"image", "video"}:
        raise ValueError("creative_experiment_identity_required")
    country = str(country_code or "").strip().upper()
    if country and (len(country) != 2 or not country.isalpha()):
        raise ValueError("invalid_creative_country_code")
    if country:
        parts.append(country)
    fingerprint = str(creative_fingerprint or "").strip().lower()
    if fingerprint and (
        len(fingerprint) != 64
        or any(ch not in "0123456789abcdef" for ch in fingerprint)
    ):
        raise ValueError("invalid_creative_fingerprint")
    if fingerprint:
        parts.append(fingerprint)
    return "cpexp_" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class VariantPerformance:
    variant_id: str
    external_ad_id: str = ""
    impressions: int = 0
    clicks: int = 0
    leads: int = 0
    bookings: int = 0
    won: int = 0
    cost_micros: int | None = None

    def normalized(self) -> "VariantPerformance":
        variant_id = str(self.variant_id or "").strip()
        if not variant_id:
            raise ValueError("variant_id_required")
        values = tuple(int(value) for value in (self.impressions, self.clicks, self.leads, self.bookings, self.won))
        if any(value < 0 for value in values):
            raise ValueError("creative_performance_must_be_non_negative")
        impressions, clicks, leads, bookings, won = values
        if not (won <= bookings <= leads <= clicks <= impressions):
            raise ValueError("creative_performance_funnel_inconsistent")
        cost = None if self.cost_micros is None else int(self.cost_micros)
        if cost is not None and cost < 0:
            raise ValueError("creative_cost_must_be_non_negative")
        return VariantPerformance(variant_id, str(self.external_ad_id or "").strip(), impressions, clicks, leads, bookings, won, cost)


@dataclass(frozen=True, slots=True)
class ExperimentEvidence:
    objective: str
    minimum_impressions: int
    eligible: tuple[str, ...]
    ranking: tuple[str, ...]
    values: dict[str, float | None]
    leader: str | None
    winner: str | None
    reason: str


def evaluate_experiment(variants: tuple[VariantPerformance, ...], *, objective: str = "booking_rate", minimum_impressions: int = 100) -> ExperimentEvidence:
    selected = str(objective or "").strip().lower()
    if selected not in _OBJECTIVES:
        raise ValueError("unsupported_creative_experiment_objective")
    threshold = max(1, int(minimum_impressions))
    normalized = tuple(item.normalized() for item in variants)
    if len({item.variant_id for item in normalized}) != len(normalized):
        raise ValueError("duplicate_creative_variant")
    eligible = tuple(item.variant_id for item in normalized if item.impressions >= threshold)
    values = {item.variant_id: _metric(item, selected) for item in normalized}
    rows = [item for item in normalized if item.variant_id in eligible and values[item.variant_id] is not None]
    reverse = selected != "cost_per_booking"
    ranking = tuple(item.variant_id for item in sorted(rows, key=lambda row: float(values[row.variant_id] or 0.0), reverse=reverse))
    leader = ranking[0] if ranking else None
    winner: str | None = None
    if not ranking:
        reason = "insufficient_observed_sample"
    elif selected not in _RATE_OBJECTIVES:
        reason = "observed_cost_leader_no_significance_claim"
    elif len(ranking) < 2:
        reason = "insufficient_comparison_sample"
    else:
        by_id = {item.variant_id: item for item in normalized}
        first = by_id[ranking[0]]
        competitors = tuple(by_id[item] for item in ranking[1:])
        if _significant_rate(first, competitors, selected):
            winner, reason = first.variant_id, "statistically_supported_observed_rate"
        else:
            reason = "observed_rate_leader_not_significant"
    return ExperimentEvidence(selected, threshold, eligible, ranking, values, leader, winner, reason)


def _count(item: VariantPerformance, objective: str) -> int:
    return {"ctr": item.clicks, "lead_rate": item.leads, "booking_rate": item.bookings, "won_rate": item.won}[objective]


def _significant_rate(first: VariantPerformance, competitors: tuple[VariantPerformance, ...], objective: str) -> bool:
    if not competitors or first.impressions <= 0: return False
    critical = NormalDist().inv_cdf(1.0 - (0.025 / len(competitors)))
    x1, n1 = _count(first, objective), first.impressions
    p1 = x1 / n1
    for second in competitors:
        n2 = second.impressions
        if n2 <= 0: return False
        x2 = _count(second, objective)
        p2 = x2 / n2
        if p1 <= p2: return False
        pooled = (x1 + x2) / (n1 + n2)
        variance = pooled * (1 - pooled) * (1 / n1 + 1 / n2)
        if variance <= 0 or (p1 - p2) / math.sqrt(variance) < critical: return False
    return True


def _metric(item: VariantPerformance, objective: str) -> float | None:
    if objective == "ctr": return _ratio(item.clicks, item.impressions)
    if objective == "lead_rate": return _ratio(item.leads, item.impressions)
    if objective == "booking_rate": return _ratio(item.bookings, item.impressions)
    if objective == "won_rate": return _ratio(item.won, item.impressions)
    return None if item.cost_micros is None or item.bookings <= 0 else item.cost_micros / item.bookings


def _ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator <= 0 else numerator / denominator


__all__ = ["ExperimentEvidence", "VariantPerformance", "evaluate_experiment", "stable_experiment_id"]
