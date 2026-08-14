from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clientplatform.infrastructure.sales_ai_analysis_repository import SalesAIAnalysisRepository
from clientplatform.infrastructure.sales_ai_job_repository import SalesAIJobRepository


_REDACTED_LIKE_PATTERN = '%"redacted":true%'


class _Cursor:
    rowcount = 0


class _CaptureConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Cursor:
        self.calls.append((sql, tuple(params)))
        return _Cursor()


def _assert_redaction_pattern_is_bound(sql: str, params: tuple[Any, ...]) -> None:
    assert "payload_json NOT LIKE ?" in sql
    assert _REDACTED_LIKE_PATTERN not in sql
    assert len(params) == 3
    assert params[-1] == _REDACTED_LIKE_PATTERN


def test_raw_message_retention_binds_like_pattern() -> None:
    conn = _CaptureConnection()

    SalesAIJobRepository(conn).purge_expired_raw_messages(
        raw_message_ttl_hours=1,
        now=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )

    _assert_redaction_pattern_is_bound(*conn.calls[-1])


def test_analysis_retention_binds_like_pattern() -> None:
    conn = _CaptureConnection()

    SalesAIAnalysisRepository(conn).purge_expired(
        analysis_ttl_days=1,
        now=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )

    _assert_redaction_pattern_is_bound(*conn.calls[-1])
