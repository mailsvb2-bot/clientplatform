from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping
from decimal import Decimal


class SalesAIIntent(StrEnum):
    SERVICE_INTEREST = "service_interest"
    PRICING = "pricing"
    BOOKING = "booking"
    SUPPORT = "support"
    COMPLAINT = "complaint"
    FOLLOW_UP = "follow_up"
    OTHER = "other"


class SalesAIReplyGoal(StrEnum):
    ANSWER_QUESTION = "answer_question"
    ASK_QUALIFICATION = "ask_qualification"
    PRESENT_OPTION = "present_option"
    HELP_CHECKOUT = "help_checkout"
    RESOLVE_ISSUE = "resolve_issue"
    HANDOFF = "handoff"
    NO_REPLY = "no_reply"


class SalesAIOfferKind(StrEnum):
    DIAGNOSTIC = "diagnostic"
    AUDIT = "audit"
    IMPLEMENTATION = "implementation"
    RECURRING = "recurring"
    NONE = "none"


def _bounded_text(value: object, *, field: str, maximum: int, allow_empty: bool = False) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return normalized


def _score(value: object, *, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number between 0 and 1")
    try:
        selected = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number between 0 and 1") from exc
    if not math.isfinite(selected) or not 0.0 <= selected <= 1.0:
        raise ValueError(f"{field} must be a finite number between 0 and 1")
    return selected


def _strict_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


@dataclass(frozen=True, slots=True)
class SalesAIAnalysis:
    intent: SalesAIIntent
    need_summary: str
    purchase_readiness: float
    confidence: float
    pricing_question: bool
    pricing_exception: bool
    need_is_specific: bool
    purchase_intent_explicit: bool
    explicit_human_request: bool
    sensitive_context: bool
    negative_sentiment: bool
    recommended_offer_kind: SalesAIOfferKind
    reply_goal: SalesAIReplyGoal
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "intent",
            self.intent if isinstance(self.intent, SalesAIIntent) else SalesAIIntent(str(self.intent)),
        )
        object.__setattr__(
            self,
            "recommended_offer_kind",
            self.recommended_offer_kind
            if isinstance(self.recommended_offer_kind, SalesAIOfferKind)
            else SalesAIOfferKind(str(self.recommended_offer_kind)),
        )
        object.__setattr__(
            self,
            "reply_goal",
            self.reply_goal
            if isinstance(self.reply_goal, SalesAIReplyGoal)
            else SalesAIReplyGoal(str(self.reply_goal)),
        )
        object.__setattr__(self, "need_summary", _bounded_text(self.need_summary, field="need_summary", maximum=600))
        object.__setattr__(self, "reason", _bounded_text(self.reason, field="reason", maximum=600))
        object.__setattr__(
            self,
            "purchase_readiness",
            _score(self.purchase_readiness, field="purchase_readiness"),
        )
        object.__setattr__(self, "confidence", _score(self.confidence, field="confidence"))
        for field in (
            "pricing_question",
            "pricing_exception",
            "need_is_specific",
            "purchase_intent_explicit",
            "explicit_human_request",
            "sensitive_context",
            "negative_sentiment",
        ):
            object.__setattr__(self, field, _strict_bool(getattr(self, field), field=field))
        if (
            self.reply_goal == SalesAIReplyGoal.HANDOFF
            and self.confidence >= 0.72
            and not (
                self.explicit_human_request
                or self.sensitive_context
                or self.pricing_exception
                or self.negative_sentiment
            )
        ):
            raise ValueError("handoff reply_goal requires a concrete handoff signal")

    def to_event_payload(self) -> dict[str, object]:
        return {
            "intent": self.intent.value,
            "need_summary": self.need_summary,
            "purchase_readiness": self.purchase_readiness,
            "confidence": self.confidence,
            "pricing_question": self.pricing_question,
            "pricing_exception": self.pricing_exception,
            "need_is_specific": self.need_is_specific,
            "purchase_intent_explicit": self.purchase_intent_explicit,
            "explicit_human_request": self.explicit_human_request,
            "sensitive_context": self.sensitive_context,
            "negative_sentiment": self.negative_sentiment,
            "recommended_offer_kind": self.recommended_offer_kind.value,
            "reply_goal": self.reply_goal.value,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SalesAIAnalysis":
        expected = {
            "intent",
            "need_summary",
            "purchase_readiness",
            "confidence",
            "pricing_question",
            "pricing_exception",
            "need_is_specific",
            "purchase_intent_explicit",
            "explicit_human_request",
            "sensitive_context",
            "negative_sentiment",
            "recommended_offer_kind",
            "reply_goal",
            "reason",
        }
        actual = set(payload)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(f"sales AI analysis keys mismatch: missing={missing}; extra={extra}")
        return cls(**{key: payload[key] for key in expected})


@dataclass(frozen=True, slots=True)
class SalesAIVerifiedOffer:
    title: str
    offering_id: str | None = None
    amount_minor: int | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _bounded_text(self.title, field="verified offer title", maximum=200))
        if self.offering_id is not None:
            value = str(self.offering_id).strip()
            if not value or len(value) > 64:
                raise ValueError("verified offering_id is invalid")
            object.__setattr__(self, "offering_id", value)
        if self.amount_minor is not None:
            if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int) or self.amount_minor <= 0:
                raise ValueError("verified offer amount_minor must be a positive integer")
        if self.currency is not None:
            currency = str(self.currency).strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("verified offer currency must be ISO-4217 alpha-3")
            object.__setattr__(self, "currency", currency)
        if (self.amount_minor is None) != (self.currency is None):
            raise ValueError("verified offer price requires both amount_minor and currency")

    @property
    def price_text(self) -> str | None:
        if self.amount_minor is None or self.currency is None:
            return None
        zero_decimal = {
            "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "PYG",
            "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
        }
        three_decimal = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
        exponent = 0 if self.currency in zero_decimal else 3 if self.currency in three_decimal else 2
        amount = Decimal(self.amount_minor) / (Decimal(10) ** exponent)
        rendered = f"{amount:.{exponent}f}" if exponent else f"{amount:.0f}"
        return f"{rendered} {self.currency}"

    def to_payload(self) -> dict[str, object]:
        return {
            "title": self.title,
            "offering_id": self.offering_id,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "price_text": self.price_text,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SalesAIVerifiedOffer":
        allowed = {"title", "offering_id", "amount_minor", "currency", "price_text"}
        if set(payload) - allowed or "title" not in payload:
            raise ValueError("verified offer keys mismatch")
        return cls(
            title=payload["title"],
            offering_id=payload.get("offering_id"),
            amount_minor=payload.get("amount_minor"),
            currency=payload.get("currency"),
        )


@dataclass(frozen=True, slots=True)
class SalesAIDraft:
    text: str
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _bounded_text(self.text, field="draft text", maximum=2500))
        object.__setattr__(self, "confidence", _score(self.confidence, field="draft confidence"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SalesAIDraft":
        if set(payload) != {"text", "confidence"}:
            raise ValueError("sales AI draft keys mismatch")
        return cls(text=payload["text"], confidence=payload["confidence"])


def sales_ai_analysis_json_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "intent": {"type": "string", "enum": [item.value for item in SalesAIIntent]},
            "need_summary": {"type": "string", "minLength": 1, "maxLength": 600},
            "purchase_readiness": {"type": "number", "minimum": 0, "maximum": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "pricing_question": {"type": "boolean"},
            "pricing_exception": {"type": "boolean"},
            "need_is_specific": {"type": "boolean"},
            "purchase_intent_explicit": {"type": "boolean"},
            "explicit_human_request": {"type": "boolean"},
            "sensitive_context": {"type": "boolean"},
            "negative_sentiment": {"type": "boolean"},
            "recommended_offer_kind": {
                "type": "string",
                "enum": [item.value for item in SalesAIOfferKind],
            },
            "reply_goal": {"type": "string", "enum": [item.value for item in SalesAIReplyGoal]},
            "reason": {"type": "string", "minLength": 1, "maxLength": 600},
        },
        "required": [
            "intent",
            "need_summary",
            "purchase_readiness",
            "confidence",
            "pricing_question",
            "pricing_exception",
            "need_is_specific",
            "purchase_intent_explicit",
            "explicit_human_request",
            "sensitive_context",
            "negative_sentiment",
            "recommended_offer_kind",
            "reply_goal",
            "reason",
        ],
    }


def sales_ai_draft_json_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 2500},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["text", "confidence"],
    }


__all__ = [
    "SalesAIAnalysis",
    "SalesAIDraft",
    "SalesAIIntent",
    "SalesAIOfferKind",
    "SalesAIReplyGoal",
    "SalesAIVerifiedOffer",
    "sales_ai_analysis_json_schema",
    "sales_ai_draft_json_schema",
]
