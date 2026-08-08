from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum


class HandoffReason(StrEnum):
    EXPLICIT_REQUEST = "explicit_request"
    LOW_CONFIDENCE = "low_confidence"
    SENSITIVE_CONTEXT = "sensitive_context"
    PRICING_EXCEPTION = "pricing_exception"
    NEGATIVE_SENTIMENT = "negative_sentiment"
    REPEATED_FAILURE = "repeated_failure"


class HandoffSeverity(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


@dataclass(frozen=True, slots=True)
class HandoffSignal:
    reason: HandoffReason
    severity: HandoffSeverity
    summary: str

    def __post_init__(self) -> None:
        reason = (
            self.reason
            if isinstance(self.reason, HandoffReason)
            else HandoffReason(str(self.reason).strip())
        )
        severity = (
            self.severity
            if isinstance(self.severity, HandoffSeverity)
            else HandoffSeverity(str(self.severity).strip())
        )
        summary = re.sub(r"\s+", " ", str(self.summary or "")).strip()
        if not summary or len(summary) > 500:
            raise ValueError("handoff summary must be 1..500 characters")
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "summary", summary)


def evaluate_handoff(
    *,
    model_confidence: float,
    explicit_human_request: bool = False,
    sensitive_context: bool = False,
    pricing_exception: bool = False,
    negative_sentiment: bool = False,
    failed_attempts: int = 0,
) -> HandoffSignal | None:
    """Return an operational handoff signal; never contact staff directly."""

    for flag_name, flag_value in (
        ("explicit_human_request", explicit_human_request),
        ("sensitive_context", sensitive_context),
        ("pricing_exception", pricing_exception),
        ("negative_sentiment", negative_sentiment),
    ):
        if not isinstance(flag_value, bool):
            raise ValueError(f"{flag_name} must be a boolean")
    confidence = float(model_confidence)
    if not math.isfinite(confidence):
        raise ValueError("model_confidence must be finite")
    confidence = max(0.0, min(confidence, 1.0))
    if isinstance(failed_attempts, bool) or not isinstance(failed_attempts, int):
        raise ValueError("failed_attempts must be a non-negative integer")
    attempts = failed_attempts
    if attempts < 0:
        raise ValueError("failed_attempts must be a non-negative integer")
    if sensitive_context:
        return HandoffSignal(
            HandoffReason.SENSITIVE_CONTEXT,
            HandoffSeverity.URGENT,
            "Sensitive or regulated context requires a human.",
        )
    if explicit_human_request:
        return HandoffSignal(
            HandoffReason.EXPLICIT_REQUEST,
            HandoffSeverity.HIGH,
            "The customer explicitly requested a human.",
        )
    if pricing_exception:
        return HandoffSignal(
            HandoffReason.PRICING_EXCEPTION,
            HandoffSeverity.HIGH,
            "The request falls outside the approved pricing/offer path.",
        )
    if attempts >= 2:
        return HandoffSignal(
            HandoffReason.REPEATED_FAILURE,
            HandoffSeverity.HIGH,
            "Automation failed repeatedly; preserve context and hand off.",
        )
    if negative_sentiment:
        return HandoffSignal(
            HandoffReason.NEGATIVE_SENTIMENT,
            HandoffSeverity.NORMAL,
            "Negative sentiment warrants human review.",
        )
    if confidence < 0.72:
        return HandoffSignal(
            HandoffReason.LOW_CONFIDENCE,
            HandoffSeverity.NORMAL,
            "Model confidence is below the sales automation threshold.",
        )
    return None


__all__ = [
    "HandoffReason",
    "HandoffSeverity",
    "HandoffSignal",
    "evaluate_handoff",
]
