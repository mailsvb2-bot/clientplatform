from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from services import jobs


class DbContext:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def __enter__(self) -> Any:
        return self.conn

    def __exit__(self, *_args: Any) -> None:
        return None


@contextmanager
def no_tx(_conn: Any):
    yield _conn


class Cursor:
    def __init__(self, *, row: Any = None, rows: list[Any] | None = None, rowcount: int = 0) -> None:
        self._row = row
        self._rows = list(rows or [])
        self.rowcount = rowcount

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class QueryConn:
    def __init__(self, handler: Callable[[str, Any], Cursor]) -> None:
        self.handler = handler
        self.calls: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = None) -> Cursor:
        normalized = " ".join(query.split())
        self.calls.append((normalized, params))
        return self.handler(normalized, params)


def test_placeholder_and_row_helpers() -> None:
    assert jobs._sql_placeholders(3) == "?,?,?"
    with pytest.raises(ValueError, match="positive"):
        jobs._sql_placeholders(0)

    assert jobs._row_get({"id": 3}, "id", 0) == 3
    assert jobs._row_get((4, 5), "id", 1) == 5
    assert jobs._row_get_optional({}, "missing", 3) is None

    claimed = jobs._claimed_jobs_from_rows(
        [
            (1, 2, "kind", "2026-07-20T00:00:00+00:00", "", "key", 3, "token"),
            {
                "id": 4,
                "user_id": 5,
                "job_type": "other",
                "run_at_utc": "later",
                "payload": None,
                "job_key": None,
                "retries": None,
                "lock_token": None,
            },
        ],
        fallback_token="fallback",
    )
    assert claimed[0] == jobs.ClaimedJob(1, 2, "kind", "2026-07-20T00:00:00+00:00", "{}", "key", 3, "token")
    assert claimed[1] == jobs.ClaimedJob(4, 5, "other", "later", "{}", "", 0, "fallback")


def test_delivery_key_and_completion_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    assert jobs._job_delivery_key_from_row((7, "kind", "key")) == (7, "job:kind:key")
    assert jobs._job_delivery_key_from_row(("bad", "kind", "key")) is None
    assert jobs._job_delivery_key_from_row((7, "", "key")) is None
    assert jobs._job_delivery_key_from_row((7, "kind", "")) is None

    monkeypatch.setattr(jobs, "_idempotency_created_at_epoch", lambda: 123)
    conn = QueryConn(lambda _q, _p: Cursor(rowcount=1))
    jobs._mark_job_delivery_done(conn, (7, "kind", "key"))
    assert conn.calls == [
        (
            "INSERT OR IGNORE INTO idempotency(user_id, key, created_at) VALUES(?,?,?)",
            (7, "job:kind:key", 123),
        )
    ]

    jobs._mark_job_delivery_done(conn, (7, "", ""))
    assert len(conn.calls) == 1


def test_release_delivery_marker_success_and_failures() -> None:
    def success_handler(query: str, _params: Any) -> Cursor:
        if query.startswith("SELECT user_id"):
            return Cursor(row=(7, "kind", "key"))
        return Cursor(rowcount=1)

    conn = QueryConn(success_handler)
    jobs._release_job_delivery_marker(conn, 9)
    assert conn.calls[-1][1] == (7, "job:kind:key")

    empty = QueryConn(lambda _q, _p: Cursor(row=None))
    jobs._release_job_delivery_marker(empty, 9)
    assert len(empty.calls) == 1

    for error_type in (RuntimeError,):
        failing = QueryConn(lambda _q, _p, error_type=error_type: (_ for _ in ()).throw(error_type("boom")))
        jobs._release_job_delivery_marker(failing, 9)


def test_add_job_sqlite_and_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "tx", no_tx)
    monkeypatch.setattr(jobs, "normalize_utc_iso", lambda value: f"norm:{value}")
    monkeypatch.setattr(jobs, "default_job_key", lambda *args: "derived-key")

    sqlite_conn = QueryConn(lambda _q, _p: Cursor(rowcount=1))
    monkeypatch.setattr(jobs, "CONFIG", SimpleNamespace(uses_postgres=False))
    monkeypatch.setattr(jobs, "db", lambda: DbContext(sqlite_conn))
    assert jobs.add_job(7, "kind", "when", {"x": "й"}) is True
    query, params = sqlite_conn.calls[0]
    assert "INSERT OR IGNORE INTO jobs" in query
    assert params == (7, "kind", "norm:when", '{"x": "й"}', "derived-key")

    sqlite_zero = QueryConn(lambda _q, _p: Cursor(rowcount=0))
    monkeypatch.setattr(jobs, "db", lambda: DbContext(sqlite_zero))
    assert jobs.add_job(7, "kind", "when", {}, job_key="fixed") is False

    postgres_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(jobs, "CONFIG", SimpleNamespace(uses_postgres=True))
    monkeypatch.setattr(jobs, "_add_job_postgres", lambda **kwargs: postgres_calls.append(kwargs) or True)
    assert jobs.add_job(8, "pg", "later", None, job_key="pg-key") is True
    assert postgres_calls[0]["run_at_utc"] == "norm:later"
    assert postgres_calls[0]["encoded_payload"] == "{}"


def test_direct_postgres_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "tx", no_tx)
    conn = QueryConn(lambda query, _params: Cursor(rowcount=1 if "INSERT INTO jobs" in query else 0))
    monkeypatch.setattr(jobs, "db", lambda: DbContext(conn))

    assert jobs._add_job_postgres(
        user_id=7,
        job_type="kind",
        run_at_utc="when",
        encoded_payload="{}",
        job_key="key",
    ) is True
    assert "pg_advisory_xact_lock" in conn.calls[0][0]
    assert "ON CONFLICT" in conn.calls[1][0]


def test_direct_postgres_claim_uses_typed_job_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "tx", no_tx)
    conn = QueryConn(lambda _query, _params: Cursor(rows=[]))
    monkeypatch.setattr(jobs, "db", lambda: DbContext(conn))

    assert jobs._claim_due_jobs_postgres(
        now_utc_iso="now",
        stale_before="stale",
        limit=2,
        token="token",
        job_type="clientplatform_booking_reminder",
    ) == []
    filtered_query, filtered_params = conn.calls[-1]
    assert "? IS NULL" not in filtered_query
    assert "AND job_type=?" in filtered_query
    assert filtered_params == [
        "now",
        "stale",
        "clientplatform_booking_reminder",
        2,
        "now",
        "token",
    ]

    assert jobs._claim_due_jobs_postgres(
        now_utc_iso="now",
        stale_before="stale",
        limit=3,
        token="all-token",
    ) == []
    unfiltered_query, unfiltered_params = conn.calls[-1]
    assert "? IS NULL" not in unfiltered_query
    assert "AND job_type=?" not in unfiltered_query
    assert unfiltered_params == ["now", "stale", 3, "now", "all-token"]


def test_cancel_jobs_and_specialized_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "tx", no_tx)
    conn = QueryConn(lambda _q, _p: Cursor(rowcount=1))
    monkeypatch.setattr(jobs, "db", lambda: DbContext(conn))

    jobs.cancel_jobs(7)
    assert conn.calls == []

    jobs.cancel_jobs(7, job_types=["a", "b"], prefix="funnel_")
    assert "job_type IN (?,?)" in conn.calls[0][0]
    assert conn.calls[0][1] == [7, "a", "b"]
    assert conn.calls[1][1] == (7, "funnel_%")

    jobs.cancel_funnel(8)
    jobs.cancel_funnel2(9)
    assert conn.calls[-2][1] == (8, "funnel_%")
    assert conn.calls[-1][1] == (9, "funnel2_%")


def test_cancel_post_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "tx", no_tx)
    conn = QueryConn(lambda _q, _p: Cursor(rowcount=1))
    monkeypatch.setattr(jobs, "db", lambda: DbContext(conn))

    jobs.cancel_post_prompt(7, "   ")
    assert conn.calls == []

    jobs.cancel_post_prompt(7, 123)
    params = conn.calls[0][1]
    assert params[0:2] == (7, "post_prompt")
    assert '"session_id":"123"' in params[2]
    assert '"session_id": "123"' in params[3]


def test_claim_due_jobs_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "normalize_utc_iso", lambda value: "2026-07-20T12:00:00+00:00")
    monkeypatch.setattr(jobs.uuid, "uuid4", lambda: SimpleNamespace(hex="token"))
    pg_calls: list[dict[str, Any]] = []
    sqlite_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(jobs, "_claim_due_jobs_postgres", lambda **kwargs: pg_calls.append(kwargs) or [])
    monkeypatch.setattr(jobs, "_claim_due_jobs_sqlite", lambda **kwargs: sqlite_calls.append(kwargs) or [])

    monkeypatch.setattr(jobs, "CONFIG", SimpleNamespace(uses_postgres=True))
    assert jobs.claim_due_jobs("raw", limit=4, lock_ttl_sec=60) == []
    assert pg_calls[0]["token"] == "token"
    assert pg_calls[0]["stale_before"] == "2026-07-20T11:59:00+00:00"

    monkeypatch.setattr(jobs, "CONFIG", SimpleNamespace(uses_postgres=False))
    assert jobs.claim_due_jobs("raw", limit=5, lock_ttl_sec=120) == []
    assert sqlite_calls[0]["limit"] == 5


def test_sqlite_claim_no_rows_and_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "tx", no_tx)
    empty = QueryConn(lambda _q, _p: Cursor(rows=[]))
    monkeypatch.setattr(jobs, "db", lambda: DbContext(empty))
    assert jobs._claim_due_jobs_sqlite(
        now_utc_iso="now", stale_before="stale", limit=2, token="token"
    ) == []

    def handler(query: str, _params: Any) -> Cursor:
        if query.startswith("SELECT id, user_id") and "lock_token" not in query:
            return Cursor(rows=[(1, 7, "kind", "when", "{}", "key", 0)])
        if query.startswith("UPDATE jobs"):
            return Cursor(rowcount=1)
        return Cursor(rows=[(1, 7, "kind", "when", "{}", "key", 0, "token")])

    conn = QueryConn(handler)
    monkeypatch.setattr(jobs, "db", lambda: DbContext(conn))
    claimed = jobs._claim_due_jobs_sqlite(
        now_utc_iso="now", stale_before="stale", limit=2, token="token"
    )
    assert claimed == [jobs.ClaimedJob(1, 7, "kind", "when", "{}", "key", 0, "token")]
    assert "id IN (?)" in conn.calls[1][0]


def test_lock_mark_done_and_reschedule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "tx", no_tx)

    success = QueryConn(lambda query, _params: Cursor(row=(1,)) if query.startswith("SELECT 1") else Cursor())
    monkeypatch.setattr(jobs, "db", lambda: DbContext(success))
    assert jobs.lock_job(1, "token") is True

    release_calls: list[int] = []
    failed = QueryConn(lambda _q, _p: Cursor(row=None))
    monkeypatch.setattr(jobs, "db", lambda: DbContext(failed))
    monkeypatch.setattr(jobs, "_release_job_delivery_marker", lambda _conn, job_id: release_calls.append(job_id))
    assert jobs.lock_job(2, "bad") is False
    assert release_calls == [2]

    no_row = QueryConn(lambda _q, _p: Cursor(row=None))
    monkeypatch.setattr(jobs, "db", lambda: DbContext(no_row))
    assert jobs.mark_done(1, "token") is False

    marker_calls: list[Any] = []
    responses = iter([Cursor(row=(7, "kind", "key")), Cursor(rowcount=1)])
    done = QueryConn(lambda _q, _p: next(responses))
    monkeypatch.setattr(jobs, "db", lambda: DbContext(done))
    monkeypatch.setattr(jobs, "utc_now_iso", lambda: "NOW")
    monkeypatch.setattr(jobs, "_mark_job_delivery_done", lambda _conn, row: marker_calls.append(row))
    assert jobs.mark_done(1, "token") is True
    assert marker_calls == [(7, "kind", "key")]

    responses = iter([Cursor(row=(7, "kind", "key")), Cursor(rowcount=1)])
    done_error = QueryConn(lambda _q, _p: next(responses))
    monkeypatch.setattr(jobs, "db", lambda: DbContext(done_error))
    marker_calls.clear()
    assert jobs.mark_done(1, "token", last_error="safe") is True
    assert marker_calls == []

    monkeypatch.setattr(jobs, "normalize_utc_iso", lambda value: f"norm:{value}")
    reschedule_conn = QueryConn(lambda _q, _p: Cursor(rowcount=1))
    monkeypatch.setattr(jobs, "db", lambda: DbContext(reschedule_conn))
    claimed = jobs.ClaimedJob(1, 7, "kind", "old", "{}", "base:a2", 2, "token")
    assert jobs.reschedule(claimed, "later", last_error="safe") is True
    assert reschedule_conn.calls[0][1] == ("norm:later", 3, "base:attempt3", "safe", 1, "token")

    retry = jobs.ClaimedJob(2, 7, "kind", "old", "{}", "", 0, "token")
    assert jobs.reschedule(retry, "later") is True
    assert reschedule_conn.calls[1][1][2] == "retry:2:attempt1"
