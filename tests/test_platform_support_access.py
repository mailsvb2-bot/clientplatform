from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from clientplatform.privacy_manifest import TENANT_POLICIES, validate_clientplatform_privacy_manifest
from services import platform_support_access as support
from services.db.schema import create_or_update_tables


class SupportDb:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            create_or_update_tables(conn)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def business(self, *, business_id: str, owner_user_id: int, name: str) -> None:
        now = "2026-09-02T10:00:00+00:00"
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO businesses(id,name,status,created_by_user_id,created_at,updated_at) VALUES(?,?,'active',?,?,?)",
                (business_id, name, owner_user_id, now, now),
            )
            conn.execute(
                "INSERT INTO business_members(id,business_id,user_id,role,status,created_at,updated_at,revoked_at) VALUES(?,?,?,'owner','active',?,?,NULL)",
                (str(uuid4()), business_id, owner_user_id, now, now),
            )

    def scalar(self, sql: str, params: tuple[object, ...] = ()) -> int:
        with self.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        assert row is not None
        return int(row[0])

    def rows(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(conn.execute(sql, params).fetchall())


@pytest.fixture
def support_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SupportDb:
    fixture = SupportDb(tmp_path / "platform-support.sqlite3")
    monkeypatch.setattr(support, "get_db", fixture.connection)
    monkeypatch.setattr(support, "is_platform_admin", lambda user_id: user_id == 9001)
    return fixture


def _issue(
    business_id: str,
    *,
    key: str = "telegram:1:1",
    now: datetime | None = None,
    reason: str = "Investigate delivery incident",
    ttl_seconds: int = 1800,
):
    return support.issue_support_session(
        9001,
        business_id=business_id,
        ticket_ref="INC-263",
        reason=reason,
        idempotency_key=key,
        ttl_seconds=ttl_seconds,
        now_utc=now or datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
    )


def test_unauthorized_operator_is_denied_before_database_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(support, "is_platform_admin", lambda _user_id: False)

    def must_not_open():
        raise AssertionError("database must not be opened for unauthorized caller")

    monkeypatch.setattr(support, "get_db", must_not_open)
    with pytest.raises(support.PlatformSupportPermissionDenied):
        support.issue_support_session(
            17,
            business_id=str(uuid4()),
            ticket_ref="INC-1",
            reason="No access",
            idempotency_key="telegram:1:2",
        )


def test_session_is_single_business_read_only_and_audited(support_db: SupportDb) -> None:
    business_id = str(uuid4())
    support_db.business(business_id=business_id, owner_user_id=101, name="North Star")
    memberships_before = support_db.scalar("SELECT COUNT(*) FROM business_members")
    issued = _issue(business_id)

    snapshot = support.read_support_business(
        9001,
        session_id=issued.id,
        business_id=business_id,
        now_utc=datetime(2026, 9, 2, 12, 5, tzinfo=UTC),
    )
    assert snapshot.business_id == business_id
    assert snapshot.business_name == "North Star"
    assert snapshot.business_status == "active"
    assert support_db.scalar("SELECT COUNT(*) FROM business_members") == memberships_before

    revoked = support.revoke_support_session(
        9001,
        session_id=issued.id,
        business_id=business_id,
        now_utc=datetime(2026, 9, 2, 12, 6, tzinfo=UTC),
    )
    assert revoked.status == "revoked"
    with pytest.raises(support.PlatformSupportSessionUnavailable):
        support.read_support_business(
            9001,
            session_id=issued.id,
            business_id=business_id,
            now_utc=datetime(2026, 9, 2, 12, 7, tzinfo=UTC),
        )

    events = support_db.rows(
        "SELECT event_type, business_id FROM clientplatform_platform_support_audit_events WHERE session_id=? ORDER BY created_at,event_type",
        (issued.id,),
    )
    assert [row["event_type"] for row in events] == [
        "issued",
        "business_metadata_read",
        "revoked",
    ]
    assert {str(row["business_id"]) for row in events} == {business_id}


def test_cross_business_reuse_fails_closed_without_read_audit(support_db: SupportDb) -> None:
    first = str(uuid4())
    second = str(uuid4())
    support_db.business(business_id=first, owner_user_id=101, name="First")
    support_db.business(business_id=second, owner_user_id=202, name="Second")
    session = _issue(first)

    with pytest.raises(
        support.PlatformSupportSessionUnavailable,
        match="business scope mismatch",
    ):
        support.read_support_business(
            9001,
            session_id=session.id,
            business_id=second,
            now_utc=datetime(2026, 9, 2, 12, 1, tzinfo=UTC),
        )

    assert support_db.scalar(
        "SELECT COUNT(*) FROM clientplatform_platform_support_audit_events WHERE event_type='business_metadata_read'"
    ) == 0


def test_expiry_is_immediate_and_session_state_remains_auditable(support_db: SupportDb) -> None:
    business_id = str(uuid4())
    support_db.business(business_id=business_id, owner_user_id=101, name="Expiry")
    start = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    session = _issue(business_id, now=start, ttl_seconds=300)

    before = support.read_support_business(
        9001,
        session_id=session.id,
        business_id=business_id,
        now_utc=start + timedelta(seconds=299),
    )
    assert before.business_id == business_id
    with pytest.raises(support.PlatformSupportSessionUnavailable):
        support.read_support_business(
            9001,
            session_id=session.id,
            business_id=business_id,
            now_utc=start + timedelta(seconds=300),
        )
    state = support.read_support_session(
        9001,
        session_id=session.id,
        business_id=business_id,
        now_utc=start + timedelta(seconds=301),
    )
    assert state.effective_status(now_utc=start + timedelta(seconds=301)) == "expired"


def test_issue_replay_is_idempotent_and_conflicting_payload_is_rejected(support_db: SupportDb) -> None:
    first = str(uuid4())
    second = str(uuid4())
    support_db.business(business_id=first, owner_user_id=101, name="First")
    support_db.business(business_id=second, owner_user_id=202, name="Second")
    initial = _issue(first, key="telegram:5:99")
    replay = _issue(
        first,
        key="telegram:5:99",
        now=datetime(2026, 9, 2, 12, 10, tzinfo=UTC),
    )
    assert replay.id == initial.id
    assert replay.issued_at == initial.issued_at
    assert replay.expires_at == initial.expires_at
    assert support_db.scalar(
        "SELECT COUNT(*) FROM clientplatform_platform_support_sessions WHERE id=?",
        (initial.id,),
    ) == 1
    assert support_db.scalar(
        "SELECT COUNT(*) FROM clientplatform_platform_support_audit_events WHERE session_id=? AND event_type='issued'",
        (initial.id,),
    ) == 1

    with pytest.raises(support.PlatformSupportSessionConflict):
        _issue(second, key="telegram:5:99")
    with pytest.raises(support.PlatformSupportSessionConflict):
        _issue(first, key="telegram:5:99", reason="Different work")


def test_session_persists_across_fresh_database_connections(support_db: SupportDb) -> None:
    business_id = str(uuid4())
    support_db.business(business_id=business_id, owner_user_id=101, name="Restart Safe")
    issued = _issue(business_id)

    # Every public service call opens a new DB connection in this fixture, which
    # models process restart/no in-memory authorization state.
    loaded = support.read_support_session(
        9001,
        session_id=issued.id,
        business_id=business_id,
        now_utc=datetime(2026, 9, 2, 12, 2, tzinfo=UTC),
    )
    assert loaded.id == issued.id
    assert loaded.business_id == business_id


def test_concurrent_same_request_creates_one_session_and_one_issue_event(support_db: SupportDb) -> None:
    business_id = str(uuid4())
    support_db.business(business_id=business_id, owner_user_id=101, name="Concurrent")
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    def issue_once(_: int) -> str:
        return _issue(business_id, key="telegram:7:700", now=now).id

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(issue_once, range(12)))

    assert len(set(ids)) == 1
    session_id = ids[0]
    assert support_db.scalar(
        "SELECT COUNT(*) FROM clientplatform_platform_support_sessions WHERE id=?",
        (session_id,),
    ) == 1
    assert support_db.scalar(
        "SELECT COUNT(*) FROM clientplatform_platform_support_audit_events WHERE session_id=? AND event_type='issued'",
        (session_id,),
    ) == 1


def test_schema_and_privacy_manifest_cover_support_capability(support_db: SupportDb) -> None:
    assert TENANT_POLICIES["clientplatform_platform_support_sessions"].disposition == "anonymize"
    assert TENANT_POLICIES["clientplatform_platform_support_audit_events"].disposition == "anonymize"
    with support_db.connection() as conn:
        report = validate_clientplatform_privacy_manifest(conn, strict=True)
    assert report.ok is True
    assert "clientplatform_platform_support_sessions" in report.discovered_business_scoped_tables
    assert "clientplatform_platform_support_audit_events" in report.discovered_business_scoped_tables
