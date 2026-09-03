from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from clientplatform.domain.tenancy import BusinessStatus, PlatformRole, normalize_user_id, normalize_uuid

PLATFORM_DIRECTORY_MAX_RESULTS = 20
PLATFORM_DIRECTORY_DEFAULT_RESULTS = 10


class PlatformDirectoryQueryKind(StrEnum):
    BUSINESS_ID = "business_id"
    USER_ID = "user_id"
    BUSINESS_NAME = "business_name"


def normalize_directory_limit(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("directory limit must be an integer")
    if isinstance(value, int):
        limit = value
    elif isinstance(value, str) and value.strip().isdigit():
        limit = int(value.strip())
    else:
        raise ValueError("directory limit must be an integer")
    if not 1 <= limit <= PLATFORM_DIRECTORY_MAX_RESULTS:
        raise ValueError(f"directory limit must be 1..{PLATFORM_DIRECTORY_MAX_RESULTS}")
    return limit


def parse_directory_query_kind(value: PlatformDirectoryQueryKind | str) -> PlatformDirectoryQueryKind:
    try:
        return (
            value if isinstance(value, PlatformDirectoryQueryKind) else PlatformDirectoryQueryKind(str(value).strip())
        )
    except ValueError as exc:
        raise ValueError("unsupported platform directory query kind") from exc


def normalize_business_name_query(value: object) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    if not 3 <= len(normalized) <= 80:
        raise ValueError("business name query must be 3..80 characters")
    if normalized in {"*", "%", "_"}:
        raise ValueError("unbounded business name query is forbidden")
    return normalized


def normalize_directory_query(
    kind: PlatformDirectoryQueryKind | str,
    value: object,
) -> tuple[PlatformDirectoryQueryKind, str | int]:
    query_kind = parse_directory_query_kind(kind)
    if query_kind == PlatformDirectoryQueryKind.BUSINESS_ID:
        return query_kind, normalize_uuid(str(value or ""), field_name="business_id")
    if query_kind == PlatformDirectoryQueryKind.USER_ID:
        return query_kind, normalize_user_id(value)  # type: ignore[arg-type]
    return query_kind, normalize_business_name_query(value)


@dataclass(frozen=True, slots=True)
class PlatformDirectoryMatch:
    business_id: str
    business_name: str
    business_status: BusinessStatus
    business_created_at: str
    active_member_count: int
    active_owner_count: int
    matched_user_id: int | None = None
    matched_role: PlatformRole | None = None
    matched_membership_status: str | None = None


@dataclass(frozen=True, slots=True)
class PlatformDirectoryLookupPage:
    matches: tuple[PlatformDirectoryMatch, ...]
    truncated: bool


__all__ = [
    "PLATFORM_DIRECTORY_DEFAULT_RESULTS",
    "PLATFORM_DIRECTORY_MAX_RESULTS",
    "PlatformDirectoryLookupPage",
    "PlatformDirectoryMatch",
    "PlatformDirectoryQueryKind",
    "escape_directory_like_literal",
    "normalize_business_name_query",
    "normalize_directory_limit",
    "normalize_directory_query",
    "parse_directory_query_kind",
]


def escape_directory_like_literal(value: str) -> str:
    """Escape SQLite/PostgreSQL LIKE metacharacters so operator input stays literal."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
