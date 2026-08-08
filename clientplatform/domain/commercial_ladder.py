from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

from clientplatform.domain.tenancy import normalize_uuid


class CommercialStepKind(StrEnum):
    DIAGNOSTIC = "diagnostic"
    AUDIT = "audit"
    IMPLEMENTATION = "implementation"
    RECURRING = "recurring"


@dataclass(frozen=True, slots=True)
class CommercialLadderStep:
    id: str
    business_id: str
    ladder_id: str
    position: int
    kind: CommercialStepKind
    title: str
    offering_id: str | None
    min_evidence_score: float
    requires_human_approval: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="ladder_step_id"))
        object.__setattr__(
            self, "business_id", normalize_uuid(self.business_id, field_name="business_id")
        )
        object.__setattr__(
            self, "ladder_id", normalize_uuid(self.ladder_id, field_name="ladder_id")
        )
        if self.offering_id is not None:
            object.__setattr__(
                self,
                "offering_id",
                normalize_uuid(self.offering_id, field_name="offering_id"),
            )
        if isinstance(self.position, bool):
            raise ValueError("commercial ladder position must be a non-negative integer")
        if isinstance(self.position, int):
            position = self.position
        elif isinstance(self.position, str) and re.fullmatch(r"[+-]?\d+", self.position.strip()):
            position = int(self.position.strip())
        else:
            raise ValueError("commercial ladder position must be a non-negative integer")
        if position < 0:
            raise ValueError("commercial ladder position must be non-negative")
        object.__setattr__(self, "position", position)
        kind = (
            self.kind
            if isinstance(self.kind, CommercialStepKind)
            else CommercialStepKind(str(self.kind).strip())
        )
        object.__setattr__(self, "kind", kind)
        title = re.sub(r"\s+", " ", str(self.title or "")).strip()
        if not title or len(title) > 200:
            raise ValueError("commercial ladder title must be 1..200 characters")
        object.__setattr__(self, "title", title)
        score = float(self.min_evidence_score)
        if not math.isfinite(score):
            raise ValueError("min_evidence_score must be finite")
        object.__setattr__(self, "min_evidence_score", max(0.0, min(score, 1.0)))
        if not isinstance(self.requires_human_approval, bool):
            raise ValueError("requires_human_approval must be a boolean")


@dataclass(frozen=True, slots=True)
class CommercialOfferCandidate:
    step_id: str
    kind: CommercialStepKind
    title: str
    offering_id: str | None
    requires_human_approval: bool


def eligible_offer_candidates(
    steps: list[CommercialLadderStep] | tuple[CommercialLadderStep, ...],
    *,
    evidence_score: float,
    completed_step_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[CommercialOfferCandidate, ...]:
    """Return ordered candidates. Selection remains an application/user decision."""

    score = float(evidence_score)
    if not math.isfinite(score):
        raise ValueError("evidence_score must be finite")
    score = max(0.0, min(score, 1.0))
    completed = {str(item) for item in completed_step_ids}
    ordered = sorted(steps, key=lambda item: (item.position, item.id))
    candidates: list[CommercialOfferCandidate] = []
    for step in ordered:
        if step.id in completed or score < step.min_evidence_score:
            continue
        candidates.append(
            CommercialOfferCandidate(
                step_id=step.id,
                kind=step.kind,
                title=step.title,
                offering_id=step.offering_id,
                requires_human_approval=step.requires_human_approval,
            )
        )
    return tuple(candidates)


__all__ = [
    "CommercialLadderStep",
    "CommercialOfferCandidate",
    "CommercialStepKind",
    "eligible_offer_candidates",
]
