from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.accounts import consolidation
from services.accounts import identity
from services.db.schema import create_or_update_tables
from services.migrations import apply_all_migrations


NOW = "2026-09-03T14:00:00+00:00"
OPERATOR = 9001
SOURCE = 20002
TARGET = 10001


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    create_or_update_tables(conn)
    apply_all_migrations(conn)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _account(conn, account_id: int, platform: str, external_id: str) -> None:
    conn.execute(
        """
        INSERT INTO accounts(account_id,primary_user_id,status,created_at,updated_at)
        VALUES(?,?,'active',?,?)
        """.strip(),
        (account_id, account_id, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO account_channel_identities(
            account_id,platform,external_user_id,linked_at,last_seen_at,link_source
        ) VALUES(?,?,?,?,?,'test')
        """.strip(),
        (account_id, platform, external_id, NOW, NOW),
    )


def _seed_pair(conn) -> None:
    _account(conn, TARGET, "telegram", "tg-target")
    _account(conn, SOURCE, "vk", "vk-source")
    conn.execute(
        """
        INSERT INTO user_channel_identities(
            user_id,platform,external_user_id,first_seen_at,last_seen_at
        ) VALUES(?,?,?,?,?)
        """.strip(),
        (TARGET, "telegram", "tg-target", NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO user_channel_identities(
            user_id,platform,external_user_id,first_seen_at,last_seen_at
        ) VALUES(?,?,?,?,?)
        """.strip(),
        (SOURCE, "vk", "vk-source", NOW, NOW),
    )


@contextmanager
def _ro(conn):
    yield conn


@contextmanager
def _atomic(conn):
    with conn:
        yield conn


@contextmanager
def _service(db_conn):
    with (
        patch.object(consolidation, "is_platform_admin", lambda uid: uid == OPERATOR),
        patch.object(consolidation, "get_db_ro", lambda: _ro(db_conn)),
        patch.object(consolidation, "atomic_db", lambda: _atomic(db_conn)),
    ):
        yield


def _plan(db_conn):
    with _service(db_conn):
        return consolidation.plan_account_consolidation(
            OPERATOR,
            source_account_id=SOURCE,
            target_account_id=TARGET,
            now_utc=datetime(2026, 9, 3, 14, 1, tzinfo=UTC),
        )


def _apply(db_conn, plan, *, key="merge-1", reason="Verified duplicate account"):
    with _service(db_conn):
        return consolidation.apply_account_consolidation(
            OPERATOR,
            source_account_id=SOURCE,
            target_account_id=TARGET,
            expected_plan_fingerprint=plan.plan_fingerprint,
            confirmation_code=plan.confirmation_code,
            idempotency_key=key,
            reason=reason,
            now_utc=datetime(2026, 9, 3, 14, 2, tzinfo=UTC),
        )


def test_unauthorized_operator_fails_before_database_open(monkeypatch) -> None:
    def must_not_open():
        raise AssertionError("database must not open")

    monkeypatch.setattr(consolidation, "is_platform_admin", lambda _uid: False)
    monkeypatch.setattr(consolidation, "get_db_ro", must_not_open)
    with pytest.raises(consolidation.AccountConsolidationPermissionDenied):
        consolidation.plan_account_consolidation(
            17, source_account_id=SOURCE, target_account_id=TARGET
        )


def test_dry_run_is_read_only_and_reports_access_expansion(db_conn) -> None:
    _seed_pair(db_conn)
    business = TenancyRepository(db_conn).create_business(
        owner_user_id=SOURCE, name="Source Business", now=NOW
    )
    db_conn.commit()
    before = db_conn.total_changes

    plan = _plan(db_conn)

    assert plan.can_apply is True
    assert plan.blockers == ()
    assert plan.source_platforms == ("vk",)
    assert plan.target_platforms == ("telegram",)
    assert [(x.business_id, x.role) for x in plan.access_expansions] == [
        (business.business.id, "owner")
    ]
    assert len(plan.plan_fingerprint) == 64
    assert plan.confirmation_code.startswith(f"MERGE-{SOURCE}-TO-{TARGET}-")
    assert db_conn.total_changes == before
    assert db_conn.execute(
        "SELECT status FROM accounts WHERE account_id=?", (SOURCE,)
    ).fetchone()["status"] == "active"


def test_apply_moves_operational_state_and_preserves_history(db_conn) -> None:
    _seed_pair(db_conn)
    tenancy = TenancyRepository(db_conn)
    business = tenancy.create_business(owner_user_id=SOURCE, name="Source Business", now=NOW)
    membership_id = business.membership.id
    business_id = business.business.id
    db_conn.execute(
        """
        INSERT INTO clientplatform_owner_control_workspaces(user_id,platform,business_id,updated_at)
        VALUES(?,?,?,?)
        """.strip(),
        (SOURCE, "vk", business_id, NOW),
    )
    db_conn.execute(
        """
        INSERT INTO clientplatform_owner_input_sessions(
            user_id,platform,surface,business_id,action,context_json,updated_at
        ) VALUES(?,?,?,?,?,?,?)
        """.strip(),
        (SOURCE, "vk", "official", business_id, "rename", "{}", NOW),
    )
    db_conn.execute(
        """
        INSERT INTO clientplatform_owner_onboarding_sessions(
            user_id,platform,step,business_id,updated_at
        ) VALUES(?,?,?,?,?)
        """.strip(),
        (SOURCE, "vk", "activity_description", business_id, NOW),
    )
    db_conn.execute(
        """
        INSERT INTO user_channel_preferences(user_id,preferred_platform,last_seen_platform,updated_at)
        VALUES(?,?,?,?)
        """.strip(),
        (SOURCE, "vk", "vk", NOW),
    )
    db_conn.execute(
        """
        INSERT INTO user_channel_bridge_tokens(
            token,user_id,purpose,created_at,account_id,target_platform,expires_at
        ) VALUES(?,?,'switch_messenger',?,?,?,?)
        """.strip(),
        ("open-token", SOURCE, NOW, SOURCE, "max", "2026-09-06T14:00:00+00:00"),
    )
    db_conn.execute(
        """
        INSERT INTO user_channel_bridge_tokens(
            token,user_id,purpose,created_at,used_at,used_platform,
            account_id,consumed_account_id,target_platform,expires_at
        ) VALUES(?,?,'switch_messenger',?,?,?,?,?,?,?)
        """.strip(),
        (
            "consumed-token",
            SOURCE,
            NOW,
            NOW,
            "max",
            SOURCE,
            SOURCE,
            "max",
            "2026-09-06T14:00:00+00:00",
        ),
    )
    db_conn.execute(
        """
        INSERT INTO user_privacy_export_tokens(token_hash,user_id,platform,created_at,consumed_at)
        VALUES(?,?,?,?,NULL)
        """.strip(),
        ("open-privacy", SOURCE, "vk", NOW),
    )
    db_conn.execute(
        """
        INSERT INTO user_privacy_export_tokens(token_hash,user_id,platform,created_at,consumed_at)
        VALUES(?,?,?,?,?)
        """.strip(),
        ("used-privacy", SOURCE, "vk", NOW, NOW),
    )
    db_conn.execute(
        """
        INSERT INTO jobs(user_id,job_type,run_at_utc,payload,job_key)
        VALUES(?,?,?,?,?)
        """.strip(),
        (SOURCE, "followup", "2026-09-04T14:00:00+00:00", "{}", "source-job"),
    )
    db_conn.execute(
        """
        INSERT INTO messenger_delivery_outbox(
            platform,external_user_id,canonical_user_id,event_key,replies_json,
            status,available_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,'pending',?,?,?)
        """.strip(),
        ("vk", "vk-source", SOURCE, "event-source", "[]", NOW, NOW, NOW),
    )
    db_conn.execute(
        "INSERT INTO idempotency(user_id,key,created_at) VALUES(?,?,?)",
        (SOURCE, "delivery:one", 1),
    )
    db_conn.execute(
        "INSERT INTO events(user_id,event,created_at) VALUES(?,?,?)",
        (SOURCE, "historical_event", NOW),
    )
    db_conn.commit()

    plan = _plan(db_conn)
    assert plan.can_apply
    result = _apply(db_conn, plan)

    source = db_conn.execute(
        "SELECT * FROM accounts WHERE account_id=?", (SOURCE,)
    ).fetchone()
    assert source["status"] == "merged"
    assert source["merged_into_account_id"] == TARGET
    assert source["merged_by_user_id"] == OPERATOR
    assert identity._resolve_canonical_account_id_in_conn(db_conn, SOURCE) == TARGET
    assert identity._resolve_canonical_user_id_in_conn(db_conn, SOURCE) == TARGET
    assert {
        (row["account_id"], row["platform"])
        for row in db_conn.execute(
            "SELECT account_id,platform FROM account_channel_identities"
        ).fetchall()
    } == {(TARGET, "telegram"), (TARGET, "vk")}
    assert {
        (row["user_id"], row["platform"])
        for row in db_conn.execute("SELECT user_id,platform FROM user_channel_identities").fetchall()
    } == {(TARGET, "telegram"), (TARGET, "vk")}
    moved_member = db_conn.execute(
        "SELECT id,user_id,role FROM business_members WHERE id=?", (membership_id,)
    ).fetchone()
    assert moved_member["user_id"] == TARGET
    assert moved_member["role"] == "owner"
    continuity = TenancyRepository(db_conn).resolve_context(
        user_id=SOURCE, business_id=business_id
    )
    assert continuity.user_id == TARGET
    assert continuity.membership_id == membership_id
    assert db_conn.execute(
        "SELECT user_id FROM clientplatform_owner_control_workspaces"
    ).fetchone()["user_id"] == TARGET
    assert db_conn.execute(
        "SELECT user_id FROM clientplatform_owner_input_sessions"
    ).fetchone()["user_id"] == TARGET
    assert db_conn.execute(
        "SELECT user_id FROM clientplatform_owner_onboarding_sessions"
    ).fetchone()["user_id"] == TARGET
    assert db_conn.execute(
        "SELECT user_id FROM jobs WHERE job_key='source-job'"
    ).fetchone()["user_id"] == TARGET
    assert db_conn.execute(
        "SELECT canonical_user_id FROM messenger_delivery_outbox WHERE event_key='event-source'"
    ).fetchone()["canonical_user_id"] == TARGET
    assert db_conn.execute(
        "SELECT user_id FROM user_channel_bridge_tokens WHERE token='open-token'"
    ).fetchone()["user_id"] == TARGET
    assert db_conn.execute(
        "SELECT account_id FROM user_channel_bridge_tokens WHERE token='open-token'"
    ).fetchone()["account_id"] == TARGET
    consumed = db_conn.execute(
        "SELECT user_id,account_id,consumed_account_id FROM user_channel_bridge_tokens WHERE token='consumed-token'"
    ).fetchone()
    assert tuple(consumed) == (SOURCE, SOURCE, SOURCE)
    assert db_conn.execute(
        "SELECT user_id FROM user_privacy_export_tokens WHERE token_hash='open-privacy'"
    ).fetchone()["user_id"] == TARGET
    assert db_conn.execute(
        "SELECT user_id FROM user_privacy_export_tokens WHERE token_hash='used-privacy'"
    ).fetchone()["user_id"] == SOURCE
    assert db_conn.execute(
        "SELECT user_id FROM events WHERE event='historical_event'"
    ).fetchone()["user_id"] == SOURCE
    assert db_conn.execute(
        "SELECT COUNT(*) c FROM account_consolidation_operations"
    ).fetchone()["c"] == 1
    assert db_conn.execute(
        "SELECT COUNT(*) c FROM account_consolidation_audit_events"
    ).fetchone()["c"] == 1
    assert result.idempotent_replay is False

    replay = _apply(db_conn, plan)
    assert replay.operation_id == result.operation_id
    assert replay.idempotent_replay is True
    assert db_conn.execute(
        "SELECT COUNT(*) c FROM account_consolidation_audit_events"
    ).fetchone()["c"] == 1


def test_identity_and_membership_collisions_fail_closed(db_conn) -> None:
    _seed_pair(db_conn)
    db_conn.execute(
        """
        INSERT INTO account_channel_identities(
            account_id,platform,external_user_id,linked_at,last_seen_at,link_source
        ) VALUES(?,?,?,?,?,'test')
        """.strip(),
        (TARGET, "max", "max-target", NOW, NOW),
    )
    db_conn.execute(
        """
        INSERT INTO account_channel_identities(
            account_id,platform,external_user_id,linked_at,last_seen_at,link_source
        ) VALUES(?,?,?,?,?,'test')
        """.strip(),
        (SOURCE, "max", "max-source", NOW, NOW),
    )
    tenancy = TenancyRepository(db_conn)
    business = tenancy.create_business(owner_user_id=SOURCE, name="Overlap", now=NOW)
    actor = tenancy.resolve_context(user_id=SOURCE, business_id=business.business.id)
    tenancy.grant_member(actor=actor, user_id=TARGET, role="support", now=NOW)
    db_conn.commit()

    plan = _plan(db_conn)

    assert plan.can_apply is False
    assert "account_channel_identity_collision:max" in plan.blockers
    assert any(item.startswith("membership_overlap:") for item in plan.blockers)


def test_inflight_oauth_job_and_outbox_block_apply(db_conn) -> None:
    _seed_pair(db_conn)
    access = TenancyRepository(db_conn).create_business(
        owner_user_id=SOURCE, name="OAuth Business", now=NOW
    )
    db_conn.execute(
        """
        INSERT INTO ad_oauth_sessions(
            state_hash,business_id,user_id,membership_id,provider,verifier_ciphertext,
            expires_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """.strip(),
        (
            "oauth-state",
            access.business.id,
            SOURCE,
            access.membership.id,
            "yandex_direct",
            "cipher",
            "2026-09-04T14:00:00+00:00",
            NOW,
        ),
    )
    db_conn.execute(
        """
        INSERT INTO jobs(user_id,job_type,run_at_utc,job_key,locked_at,lock_token)
        VALUES(?,?,?,?,?,?)
        """.strip(),
        (SOURCE, "locked", NOW, "locked-job", NOW, "lock-1"),
    )
    db_conn.execute(
        """
        INSERT INTO messenger_delivery_outbox(
            platform,external_user_id,canonical_user_id,event_key,replies_json,
            status,available_at,locked_at,lock_token,created_at,updated_at
        ) VALUES(?,?,?,?,?,'sending',?,?,?,?,?)
        """.strip(),
        ("vk", "vk-source", SOURCE, "sending-event", "[]", NOW, NOW, "send-lock", NOW, NOW),
    )
    db_conn.commit()

    plan = _plan(db_conn)

    assert plan.can_apply is False
    assert "active_ad_oauth_sessions:1" in plan.blockers
    assert "locked_active_jobs:1" in plan.blockers
    assert "sending_outbox_rows:1" in plan.blockers


def test_stale_plan_rolls_back_without_partial_merge(db_conn) -> None:
    _seed_pair(db_conn)
    db_conn.commit()
    plan = _plan(db_conn)
    db_conn.execute(
        "INSERT INTO idempotency(user_id,key,created_at) VALUES(?,?,?)",
        (SOURCE, "new-after-plan", 1),
    )
    db_conn.commit()

    with pytest.raises(consolidation.AccountConsolidationStalePlan):
        _apply(db_conn, plan)

    source = db_conn.execute(
        "SELECT status,merged_into_account_id FROM accounts WHERE account_id=?", (SOURCE,)
    ).fetchone()
    assert tuple(source) == ("active", None)
    assert db_conn.execute(
        "SELECT account_id FROM account_channel_identities WHERE platform='vk'"
    ).fetchone()["account_id"] == SOURCE
    assert db_conn.execute(
        "SELECT COUNT(*) c FROM account_consolidation_operations"
    ).fetchone()["c"] == 0


def test_confirmation_and_idempotency_conflicts_are_fail_closed(db_conn) -> None:
    _seed_pair(db_conn)
    db_conn.commit()
    plan = _plan(db_conn)

    with _service(db_conn):
        with pytest.raises(consolidation.AccountConsolidationConflict):
            consolidation.apply_account_consolidation(
                OPERATOR,
                source_account_id=SOURCE,
                target_account_id=TARGET,
                expected_plan_fingerprint=plan.plan_fingerprint,
                confirmation_code="MERGE-wrong-code",
                idempotency_key="merge-confirmation",
                reason="Verified duplicate account",
            )
    assert db_conn.execute(
        "SELECT status FROM accounts WHERE account_id=?", (SOURCE,)
    ).fetchone()["status"] == "active"

    _apply(db_conn, plan, key="same-key", reason="Verified duplicate account")
    with _service(db_conn):
        with pytest.raises(consolidation.AccountConsolidationConflict):
            consolidation.apply_account_consolidation(
                OPERATOR,
                source_account_id=SOURCE,
                target_account_id=TARGET,
                expected_plan_fingerprint=plan.plan_fingerprint,
                confirmation_code=plan.confirmation_code,
                idempotency_key="same-key",
                reason="Different merge reason",
            )


def test_unknown_future_identity_dependency_blocks_plan(db_conn) -> None:
    _seed_pair(db_conn)
    db_conn.execute(
        "CREATE TABLE future_identity_surface(id INTEGER PRIMARY KEY, future_user_id INTEGER NOT NULL)"
    )
    db_conn.execute(
        "INSERT INTO future_identity_surface(id,future_user_id) VALUES(1,?)", (SOURCE,)
    )
    db_conn.commit()

    plan = _plan(db_conn)

    assert plan.can_apply is False
    assert "unknown_identity_dependency:future_identity_surface.future_user_id:1" in plan.blockers


def test_alias_cycle_and_missing_target_fail_closed(db_conn) -> None:
    _seed_pair(db_conn)
    db_conn.execute(
        "UPDATE accounts SET status='merged',merged_into_account_id=? WHERE account_id=?",
        (TARGET, SOURCE),
    )
    db_conn.execute(
        "UPDATE accounts SET status='merged',merged_into_account_id=? WHERE account_id=?",
        (SOURCE, TARGET),
    )
    db_conn.commit()
    with pytest.raises(identity.AccountIdentityMergeInvariantError, match="cycle"):
        identity._resolve_canonical_account_id_in_conn(db_conn, SOURCE)

    db_conn.execute(
        "UPDATE accounts SET merged_into_account_id=? WHERE account_id=?", (999999, SOURCE)
    )
    db_conn.execute(
        "UPDATE accounts SET status='active',merged_into_account_id=NULL WHERE account_id=?",
        (TARGET,),
    )
    db_conn.commit()
    with pytest.raises(identity.AccountIdentityMergeInvariantError, match="missing"):
        identity._resolve_canonical_account_id_in_conn(db_conn, SOURCE)
