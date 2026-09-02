from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from clientplatform.application import support_cases as application
from clientplatform.domain.support_cases import SupportCaseStatus
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.support_case_repository import (
    SupportCaseConflict,
    SupportCaseRepository,
    SupportCaseUnavailable,
)
from clientplatform.privacy_manifest import TENANT_POLICIES, validate_clientplatform_privacy_manifest
from services.db.schema import create_or_update_tables


@pytest.fixture
def case_store():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    create_or_update_tables(conn)
    tenancy = TenancyRepository(conn)
    first = tenancy.create_business(owner_user_id=101, name="First")
    second = tenancy.create_business(owner_user_id=202, name="Second")
    actor_first = tenancy.resolve_context(user_id=101, business_id=first.business.id)
    actor_second = tenancy.resolve_context(user_id=202, business_id=second.business.id)
    try:
        yield conn, SupportCaseRepository(conn), actor_first, actor_second
    finally:
        conn.close()


def test_summary_rejects_provider_credentials(case_store) -> None:
    _conn, repo, actor, _other = case_store
    long_secret = "1234" * 8
    alpha_secret = "abcdefgh" * 4
    telegram_secret = "ABCDEFGHIJ" * 4
    jwt_part = "abcdefghijk"
    for summary in (
        "api_key=" + long_secret,
        "Authorization: " + alpha_secret,
        "Bearer " + alpha_secret,
        "123456789:" + telegram_secret,
        "eyJ" + jwt_part + "." + jwt_part + "." + jwt_part,
    ):
        with pytest.raises(ValueError, match="credentials or secrets"):
            repo.create(
                actor=actor,
                category="technical",
                summary=summary,
                idempotency_key=f"secret-{len(summary)}-{summary[:4]}",
            )


def test_create_is_idempotent_and_audited_once(case_store) -> None:
    conn, repo, actor, _other = case_store
    first = repo.create(
        actor=actor,
        category="technical",
        summary="Cannot connect messenger",
        idempotency_key="telegram:1:10",
        now="2026-09-02T16:00:00+00:00",
    )
    replay = repo.create(
        actor=actor,
        category="technical",
        summary="Cannot connect messenger",
        idempotency_key="telegram:1:10",
        now="2026-09-02T16:01:00+00:00",
    )
    assert replay.id == first.id
    count = conn.execute(
        "SELECT COUNT(*) FROM clientplatform_support_case_audit_events WHERE case_id=? AND event_type='created'",
        (first.id,),
    ).fetchone()[0]
    assert count == 1


def test_create_idempotency_conflict_fails_closed(case_store) -> None:
    _conn, repo, actor, _other = case_store
    repo.create(actor=actor, category="general", summary="First summary", idempotency_key="same")
    with pytest.raises(SupportCaseConflict):
        repo.create(actor=actor, category="security", summary="Different work", idempotency_key="same")


def test_tenant_list_is_business_scoped(case_store) -> None:
    _conn, repo, first, second = case_store
    one = repo.create(actor=first, category="general", summary="First tenant case", idempotency_key="1")
    two = repo.create(actor=second, category="billing", summary="Second tenant case", idempotency_key="2")
    assert [item.id for item in repo.list_for_tenant(actor=first)] == [one.id]
    assert [item.id for item in repo.list_for_tenant(actor=second)] == [two.id]


def test_claim_release_resolve_lifecycle_and_queue(case_store) -> None:
    conn, repo, actor, _other = case_store
    case = repo.create(actor=actor, category="integration", summary="Provider callback issue", idempotency_key="new")
    claimed = repo.claim_platform(operator_user_id=9001, case_id=case.id, idempotency_key="claim")
    assert claimed.status == SupportCaseStatus.CLAIMED
    assert claimed.claimed_by_operator_user_id == 9001
    released = repo.release_platform(operator_user_id=9001, case_id=case.id, idempotency_key="release")
    assert released.status == SupportCaseStatus.OPEN
    claimed_again = repo.claim_platform(operator_user_id=9001, case_id=case.id, idempotency_key="claim-2")
    resolved = repo.resolve_platform(operator_user_id=9001, case_id=case.id, idempotency_key="resolve")
    assert claimed_again.status == SupportCaseStatus.CLAIMED
    assert resolved.status == SupportCaseStatus.RESOLVED
    assert repo.list_platform_queue() == []
    events = [row[0] for row in conn.execute(
        "SELECT event_type FROM clientplatform_support_case_audit_events WHERE case_id=? ORDER BY created_at,event_type",
        (case.id,),
    ).fetchall()]
    assert set(events) == {"created", "claimed", "released", "resolved"}


def test_other_operator_cannot_release_or_resolve(case_store) -> None:
    _conn, repo, actor, _other = case_store
    case = repo.create(actor=actor, category="general", summary="Need support", idempotency_key="case")
    repo.claim_platform(operator_user_id=9001, case_id=case.id, idempotency_key="claim")
    with pytest.raises(SupportCaseConflict):
        repo.release_platform(operator_user_id=9002, case_id=case.id, idempotency_key="release-other")
    with pytest.raises(SupportCaseConflict):
        repo.resolve_platform(operator_user_id=9002, case_id=case.id, idempotency_key="resolve-other")


def test_new_operation_key_cannot_masquerade_as_claim_or_resolve_replay(case_store) -> None:
    _conn, repo, actor, _other = case_store
    case = repo.create(actor=actor, category="general", summary="Need support", idempotency_key="case-replay")
    repo.claim_platform(operator_user_id=9001, case_id=case.id, idempotency_key="claim-1")
    with pytest.raises(SupportCaseConflict, match="already claimed"):
        repo.claim_platform(operator_user_id=9001, case_id=case.id, idempotency_key="claim-2")
    resolved = repo.resolve_platform(operator_user_id=9001, case_id=case.id, idempotency_key="resolve-1")
    assert resolved.status == SupportCaseStatus.RESOLVED
    with pytest.raises(SupportCaseUnavailable, match="already resolved"):
        repo.resolve_platform(operator_user_id=9001, case_id=case.id, idempotency_key="resolve-2")


def test_stale_claim_replay_fails_after_release(case_store) -> None:
    _conn, repo, actor, _other = case_store
    case = repo.create(actor=actor, category="general", summary="Need support", idempotency_key="case")
    repo.claim_platform(operator_user_id=9001, case_id=case.id, idempotency_key="claim")
    repo.release_platform(operator_user_id=9001, case_id=case.id, idempotency_key="release")
    with pytest.raises(SupportCaseUnavailable, match="replay is stale"):
        repo.claim_platform(operator_user_id=9001, case_id=case.id, idempotency_key="claim")


def test_platform_gate_denies_before_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(application, "is_platform_admin", lambda _user_id: False)

    def must_not_open():
        raise AssertionError("database must not open")

    monkeypatch.setattr(application, "get_db_ro", must_not_open)
    with pytest.raises(application.PlatformSupportCasePermissionDenied):
        application.list_platform_support_queue(17)


def test_case_to_support_session_requires_exact_claim_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    case = SimpleNamespace(
        id="f3b3c9dd-fcb1-43ad-b911-32dfd81222ac",
        business_id="ad67e150-0d91-48c9-a879-44a44782250d",
        status=SupportCaseStatus.CLAIMED,
        claimed_by_operator_user_id=9001,
    )
    connection = object()

    @contextmanager
    def fake_db():
        yield connection

    class FakeRepo:
        def __init__(self, conn):
            assert conn is connection

        def require_claimed_for_platform_session(
            self, *, operator_user_id: int, case_id: str
        ):
            assert case_id == case.id
            if case.status != SupportCaseStatus.CLAIMED:
                raise SupportCaseUnavailable("support case must be claimed")
            if case.claimed_by_operator_user_id != operator_user_id:
                raise SupportCaseConflict("support case is owned by another operator")
            return case

    captured = {}
    monkeypatch.setattr(application, "is_platform_admin", lambda user_id: user_id == 9001)
    monkeypatch.setattr(application, "get_db", fake_db)
    monkeypatch.setattr(application, "SupportCaseRepository", FakeRepo)

    def fake_issue(user_id, *, conn, **kwargs):
        assert conn is connection
        captured.update(user_id=user_id, **kwargs)
        return "session"

    monkeypatch.setattr(application, "issue_support_session_in_transaction", fake_issue)
    result = application.issue_support_session_for_case(
        9001,
        case_id=case.id,
        reason="Investigate exact case",
        idempotency_key="telegram:1:99",
    )
    assert result == "session"
    assert captured["business_id"] == case.business_id
    assert captured["ticket_ref"] == f"support-case:{case.id}"

    case.claimed_by_operator_user_id = 9002
    with pytest.raises(SupportCaseConflict, match="another operator"):
        application.issue_support_session_for_case(
            9001,
            case_id=case.id,
            reason="No",
            idempotency_key="telegram:1:100",
        )


def test_privacy_manifest_classifies_support_case_tables(case_store) -> None:
    conn, _repo, _actor, _other = case_store
    assert TENANT_POLICIES["clientplatform_support_cases"].disposition == "anonymize"
    assert TENANT_POLICIES["clientplatform_support_case_audit_events"].disposition == "anonymize"
    report = validate_clientplatform_privacy_manifest(conn, strict=True)
    assert report.ok is True
    assert "clientplatform_support_cases" in report.discovered_business_tables
    assert "clientplatform_support_case_audit_events" in report.discovered_business_tables
