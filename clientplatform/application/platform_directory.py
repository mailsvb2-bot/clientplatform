from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from clientplatform.domain.platform_directory import (
    PLATFORM_DIRECTORY_DEFAULT_RESULTS,
    PlatformDirectoryMatch,
    PlatformDirectoryQueryKind,
    normalize_directory_limit,
    normalize_directory_query,
)
from clientplatform.infrastructure.platform_operator_audit_repository import (
    PlatformOperatorAuditRepository,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.admin import is_platform_admin
from services.db import get_db


class PlatformDirectoryPermissionDenied(PermissionError):
    """The caller is not an explicitly configured platform operator."""


@dataclass(frozen=True, slots=True)
class PlatformDirectorySearchResult:
    query_kind: PlatformDirectoryQueryKind
    matches: tuple[PlatformDirectoryMatch, ...]
    truncated: bool
    audit_id: str
    searched_at: str


def _operator(user_id: int | None) -> int:
    if user_id is None or not is_platform_admin(user_id):
        raise PlatformDirectoryPermissionDenied("platform directory access required")
    return int(user_id)


def _clock(now_utc: datetime | None) -> datetime:
    current = now_utc or datetime.now(tz=UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    return current.astimezone(UTC)


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _result_fingerprint(matches: tuple[PlatformDirectoryMatch, ...], *, truncated: bool) -> str:
    return _sha256_json(
        {
            "matches": [
                {
                    "business_id": item.business_id,
                    "business_status": item.business_status.value,
                    "matched_membership_status": item.matched_membership_status,
                    "matched_role": None if item.matched_role is None else item.matched_role.value,
                    "matched_user_id": item.matched_user_id,
                }
                for item in matches
            ],
            "truncated": truncated,
        }
    )


def search_platform_directory(
    user_id: int | None,
    *,
    query_kind: PlatformDirectoryQueryKind | str,
    query: object,
    limit: int = PLATFORM_DIRECTORY_DEFAULT_RESULTS,
    now_utc: datetime | None = None,
) -> PlatformDirectorySearchResult:
    operator_user_id = _operator(user_id)
    kind, normalized_query = normalize_directory_query(query_kind, query)
    bounded_limit = normalize_directory_limit(limit)
    searched_at = _clock(now_utc).isoformat()
    query_fingerprint = _sha256_json({"kind": kind.value, "query": normalized_query})

    with get_db() as conn:
        lookup = TenancyRepository(conn).lookup_platform_directory(
            query_kind=kind,
            query=normalized_query,
            limit=bounded_limit,
        )
        audit = PlatformOperatorAuditRepository(conn).record_directory_lookup(
            operator_user_id=operator_user_id,
            query_kind=kind,
            query_fingerprint=query_fingerprint,
            result_count=len(lookup.matches),
            result_fingerprint=_result_fingerprint(lookup.matches, truncated=lookup.truncated),
            created_at=searched_at,
        )
    return PlatformDirectorySearchResult(
        query_kind=kind,
        matches=lookup.matches,
        truncated=lookup.truncated,
        audit_id=audit.id,
        searched_at=searched_at,
    )


__all__ = [
    "PlatformDirectoryPermissionDenied",
    "PlatformDirectorySearchResult",
    "search_platform_directory",
]
