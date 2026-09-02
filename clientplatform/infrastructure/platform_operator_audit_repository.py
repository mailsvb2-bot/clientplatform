from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from clientplatform.domain.platform_directory import (
    PLATFORM_DIRECTORY_MAX_RESULTS,
    PlatformDirectoryQueryKind,
    parse_directory_query_kind,
)
from clientplatform.domain.tenancy import normalize_user_id

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PlatformOperatorAuditEvent:
    id: str
    operator_user_id: int
    action: str
    query_kind: PlatformDirectoryQueryKind
    query_fingerprint: str
    result_count: int
    result_fingerprint: str
    created_at: str


def _fingerprint(value: object, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if _HEX64.fullmatch(normalized) is None:
        raise ValueError(f"{field} must be a lowercase sha256 fingerprint")
    return normalized


class PlatformOperatorAuditRepository:
    """Append-only evidence for high-trust platform-operator navigation."""

    def __init__(self, conn: Any):
        self._conn = conn

    def record_directory_lookup(
        self,
        *,
        operator_user_id: int,
        query_kind: PlatformDirectoryQueryKind | str,
        query_fingerprint: str,
        result_count: int,
        result_fingerprint: str,
        created_at: str,
    ) -> PlatformOperatorAuditEvent:
        operator = normalize_user_id(operator_user_id)
        kind = parse_directory_query_kind(query_kind)
        query_hash = _fingerprint(query_fingerprint, field="query_fingerprint")
        result_hash = _fingerprint(result_fingerprint, field="result_fingerprint")
        if isinstance(result_count, bool) or not isinstance(result_count, int):
            raise ValueError("directory audit result_count must be an integer")
        count = result_count
        if not 0 <= count <= PLATFORM_DIRECTORY_MAX_RESULTS:
            raise ValueError("directory audit result_count is outside hard cap")
        timestamp = str(created_at or "").strip()
        if not timestamp:
            raise ValueError("directory audit created_at is required")
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ValueError("directory audit created_at must be ISO-8601") from exc
        if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
            raise ValueError("directory audit created_at must be timezone-aware")
        event_id = str(uuid4())
        self._conn.execute(
            """
            INSERT INTO clientplatform_platform_operator_audit_events(
                id, operator_user_id, action, query_kind, query_fingerprint,
                result_count, result_fingerprint, created_at
            ) VALUES(?, ?, 'directory_lookup', ?, ?, ?, ?, ?)
            """,
            (event_id, operator, kind.value, query_hash, count, result_hash, timestamp),
        )
        return PlatformOperatorAuditEvent(
            id=event_id,
            operator_user_id=operator,
            action="directory_lookup",
            query_kind=kind,
            query_fingerprint=query_hash,
            result_count=count,
            result_fingerprint=result_hash,
            created_at=timestamp,
        )
