from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID



_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|secret|password|authorization)\s*[:=]\s*\S{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~-]{16,}"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)

class SupportCaseCategory(StrEnum):
    GENERAL = "general"
    BILLING = "billing"
    TECHNICAL = "technical"
    SECURITY = "security"
    INTEGRATION = "integration"


class SupportCaseStatus(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    RESOLVED = "resolved"


def normalize_support_case_id(value: object) -> str:
    try:
        return str(UUID(str(value or "").strip()))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("support_case_id must be a valid UUID") from exc


def normalize_support_category(value: SupportCaseCategory | str) -> SupportCaseCategory:
    try:
        return value if isinstance(value, SupportCaseCategory) else SupportCaseCategory(str(value).strip())
    except ValueError as exc:
        raise ValueError("unsupported support case category") from exc


def normalize_support_summary(value: object) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    if not 3 <= len(normalized) <= 1000:
        raise ValueError("support case summary must be 3..1000 characters")
    if any(pattern.search(normalized) for pattern in _CREDENTIAL_PATTERNS):
        raise ValueError("support case summary must not contain credentials or secrets")
    return normalized


@dataclass(frozen=True, slots=True)
class SupportCase:
    id: str
    business_id: str
    category: SupportCaseCategory
    summary: str
    status: SupportCaseStatus
    created_by_member_id: str
    claimed_by_operator_user_id: int | None
    created_at: str
    updated_at: str
    claimed_at: str | None
    resolved_at: str | None


__all__ = [
    "SupportCase",
    "SupportCaseCategory",
    "SupportCaseStatus",
    "normalize_support_case_id",
    "normalize_support_category",
    "normalize_support_summary",
]
