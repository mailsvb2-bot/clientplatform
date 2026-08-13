from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from clientplatform.domain.sales_intelligence import (
    SalesAIAnalysis,
    SalesAIIntent,
    SalesAIOfferKind,
    SalesAIReplyGoal,
)
from clientplatform.domain.sales_state_machine import SalesConversationEvent


class SalesAIDataMode(StrEnum):
    REDACTED = "redacted"
    STANDARD = "standard"
    NO_CLOUD = "no_cloud"


_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?7|8)?[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_LONG_NUMBER_RE = re.compile(r"(?<!\d)\d{8,19}(?!\d)")
_HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{5,32}\b")
_URL_TOKEN_RE = re.compile(r"(?i)https?://[^\s]+")


@dataclass(frozen=True, slots=True)
class SalesAITextPreparation:
    text: str
    redacted: bool


def normalize_sales_ai_data_mode(value: SalesAIDataMode | str) -> SalesAIDataMode:
    return value if isinstance(value, SalesAIDataMode) else SalesAIDataMode(str(value or "").strip())


def prepare_sales_ai_text(text: str, *, mode: SalesAIDataMode | str) -> SalesAITextPreparation:
    selected = normalize_sales_ai_data_mode(mode)
    normalized = " ".join(str(text or "").replace("\x00", " ").split())
    if not normalized:
        raise ValueError("sales AI customer text must not be empty")
    if selected == SalesAIDataMode.NO_CLOUD:
        raise PermissionError("sales AI cloud egress is disabled for this business")
    if selected == SalesAIDataMode.STANDARD:
        return SalesAITextPreparation(text=normalized, redacted=False)

    redacted = normalized
    for pattern, marker in (
        (_EMAIL_RE, "[EMAIL]"),
        (_PHONE_RE, "[PHONE]"),
        (_LONG_NUMBER_RE, "[NUMBER]"),
        (_HANDLE_RE, "[HANDLE]"),
        (_URL_TOKEN_RE, "[URL]"),
    ):
        redacted = pattern.sub(marker, redacted)
    return SalesAITextPreparation(text=redacted, redacted=redacted != normalized)


def validated_sales_ai_milestones(analysis: SalesAIAnalysis) -> tuple[SalesConversationEvent, ...]:
    """Convert bounded observations into conservative deterministic milestones.

    AI cannot assert payment, checkout, offer presentation or loss. These two
    milestones are permitted only when multiple independent structured signals
    agree and confidence is high. The state repository still validates the actual
    transition and fails closed on an impossible jump.
    """

    if (
        analysis.confidence < 0.90
        or analysis.explicit_human_request
        or analysis.sensitive_context
        or analysis.pricing_exception
        or analysis.negative_sentiment
    ):
        return ()

    milestones: list[SalesConversationEvent] = []
    if (
        analysis.need_is_specific
        and analysis.intent
        in {SalesAIIntent.SERVICE_INTEREST, SalesAIIntent.PRICING, SalesAIIntent.BOOKING}
    ):
        milestones.append(SalesConversationEvent.NEED_CAPTURED)

    if (
        analysis.need_is_specific
        and analysis.purchase_intent_explicit
        and analysis.purchase_readiness >= 0.75
        and analysis.recommended_offer_kind != SalesAIOfferKind.NONE
        and analysis.reply_goal in {
            SalesAIReplyGoal.PRESENT_OPTION,
            SalesAIReplyGoal.ANSWER_QUESTION,
            SalesAIReplyGoal.HELP_CHECKOUT,
        }
    ):
        milestones.append(SalesConversationEvent.QUALIFICATION_PASSED)
    return tuple(milestones)


__all__ = [
    "SalesAIDataMode",
    "SalesAITextPreparation",
    "normalize_sales_ai_data_mode",
    "prepare_sales_ai_text",
    "validated_sales_ai_milestones",
]
