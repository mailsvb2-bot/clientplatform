from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3
import unittest
from unittest.mock import patch

from clientplatform.application import event_followup_settings as settings_app
from clientplatform.infrastructure.automation_policy_repository import AutomationPolicyRepository
from clientplatform.infrastructure.event_followup_settings_repository import (
    EventFollowupSettingsRepository,
    event_followups_enabled_in_conn,
    event_followups_platform_enabled,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import create_or_update_tables


@contextmanager
def _shared(conn: sqlite3.Connection):
    yield conn


def _owner_db() -> tuple[sqlite3.Connection, object]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    create_or_update_tables(conn)
    access = TenancyRepository(conn).create_business(
        owner_user_id=701,
        name="Autosend Settings",
        now="2026-09-12T12:00:00+00:00",
    )
    owner = TenancyRepository(conn).resolve_context(
        user_id=701,
        business_id=access.business.id,
    )
    return conn, owner


def _seed_pending_email_followup(
    conn: sqlite3.Connection,
    owner: object,
    *,
    registration_id: str,
    dispatch_id: str,
    no_show: bool,
) -> None:
    stamp = "2026-09-12T10:00:00+00:00"
    connection_id = f"email-{dispatch_id}"
    event_id = f"event-{dispatch_id}"
    conn.execute(
        """
        INSERT INTO connections(
            id,business_id,platform,connection_type,external_account_id,
            credential_reference,status,created_by_member_id,created_at,updated_at
        ) VALUES(?,?,'email','email_smtp',?,'secret://email','active',?,?,?)
        """,
        (connection_id, owner.business_id, connection_id, owner.membership_id, stamp, stamp),
    )
    conn.execute(
        """
        INSERT INTO clientplatform_events(
            id,business_id,created_by_member_id,kind,status,title,description,
            starts_at,ends_at,timezone_name,provider_key,join_url,offer_url,
            public_slug,consent_version,notification_connection_id,created_at,updated_at
        ) VALUES(?,?,?,'webinar','completed','Тест','',?,?, 'Europe/Moscow',
                 'external','https://example.test/join','https://example.test/offer',
                 ?,'v1',?,?,?)
        """,
        (
            event_id, owner.business_id, owner.membership_id,
            "2026-09-12T08:00:00+00:00", "2026-09-12T09:00:00+00:00",
            f"public-slug-{dispatch_id}-1234567890", connection_id, stamp, stamp,
        ),
    )
    attendance = None if no_show else "2026-09-12T08:10:00+00:00"
    conn.execute(
        """
        INSERT INTO clientplatform_event_registrations(
            id,event_id,business_id,status,name,email,identity_hash,token,
            consent_version,consented_at,registered_at,attendance_confirmed_at
        ) VALUES(?,?,?,'registered','Иван','ivan@example.test',?,?, 'v1',?,?,?)
        """,
        (
            registration_id, event_id, owner.business_id, "a" * 64,
            f"token-{dispatch_id}-12345678901234567890123456789012",
            stamp, stamp, attendance,
        ),
    )
    conn.execute(
        """
        INSERT INTO provider_dispatch_outbox(
            id,business_id,platform,source_kind,source_id,connection_id,
            recipient_kind,external_subject,payload_kind,payload_ref,
            idempotency_key,status,attempts,available_at,created_at,updated_at
        ) VALUES(?,?,'email','event_message',?,?,'external_subject','ivan@example.test',
                 'mixed','{}',?,'pending',0,?,?,?)
        """,
        (
            dispatch_id, owner.business_id, registration_id, connection_id,
            f"event:{event_id}:registration:{registration_id}:message:post:v4:stage:1",
            stamp, stamp, stamp,
        ),
    )
    conn.commit()


def test_owner_can_enable_and_disable_event_autosend(monkeypatch) -> None:
    conn, owner = _owner_db()
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)

    with (
        patch.object(settings_app, "get_db", side_effect=lambda: _shared(conn)),
        patch.object(settings_app, "get_db_ro", side_effect=lambda: _shared(conn)),
    ):
        assert settings_app.get_business_event_followups_enabled(actor=owner) is False
        assert settings_app.set_business_event_followups_enabled(
            actor=owner,
            enabled=True,
            now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
        ) is True
        assert settings_app.get_business_event_followups_enabled(actor=owner) is True

        stored = EventFollowupSettingsRepository(conn).get(business_id=owner.business_id)
        assert stored is not None and stored.enabled is True and stored.settings_epoch == 1
        policy = AutomationPolicyRepository(conn).effective(
            actor=owner,
            now="2026-09-12T12:01:00+00:00",
        )
        assert policy is not None
        assert "events.commercial_followup" in policy.spec.allowed_actions
        assert policy.spec.allowed_channels == ()
        assert policy.spec.allowed_audiences == ()
        event_scope = next(
            scope for scope in policy.spec.action_scopes
            if scope.action == "events.commercial_followup"
        )
        assert event_scope.allowed_channels == ("email", "max", "vk")
        assert event_scope.allowed_audiences == ("prospect_opted_in",)
        assert event_scope.allowed_content_topics == ("service_offer",)
        assert event_scope.schedule is not None
        assert event_scope.schedule.quiet_start == "22:00"
        assert event_scope.schedule.quiet_end == "08:00"

        assert settings_app.set_business_event_followups_enabled(
            actor=owner,
            enabled=False,
            now=datetime(2026, 9, 12, 12, 2, tzinfo=timezone.utc),
        ) is False
        stored = EventFollowupSettingsRepository(conn).get(business_id=owner.business_id)
        assert stored is not None and stored.enabled is False and stored.settings_epoch == 2
        assert event_followups_enabled_in_conn(conn, business_id=owner.business_id) is False

    actions = [
        row[0]
        for row in conn.execute(
            "SELECT action FROM clientplatform_admin_audit_events "
            "WHERE business_id=? ORDER BY created_at,id",
            (owner.business_id,),
        ).fetchall()
        if str(row[0]).startswith("event_commercial_followups_")
    ]
    assert actions == [
        "event_commercial_followups_enabled",
        "event_commercial_followups_disabled",
    ]
    conn.close()


def test_platform_kill_switch_blocks_owner_enable(monkeypatch) -> None:
    conn, owner = _owner_db()
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "false")
    assert event_followups_platform_enabled() is False
    with patch.object(settings_app, "get_db", side_effect=lambda: _shared(conn)):
        with unittest.TestCase().assertRaisesRegex(ValueError, "уровне платформы"):
            settings_app.set_business_event_followups_enabled(
                actor=owner,
                enabled=True,
                now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
            )
    assert EventFollowupSettingsRepository(conn).get(business_id=owner.business_id) is None
    conn.close()


def test_followup_toggle_preserves_growth_autopilot_policy(monkeypatch) -> None:
    conn, owner = _owner_db()
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)
    with (
        patch.object(settings_app, "get_db", side_effect=lambda: _shared(conn)),
        patch.object(settings_app, "get_db_ro", side_effect=lambda: _shared(conn)),
    ):
        from clientplatform.application import automation_policy as automation_app

        with (
            patch.object(automation_app, "get_db", side_effect=lambda: _shared(conn)),
            patch.object(automation_app, "get_db_ro", side_effect=lambda: _shared(conn)),
        ):
            automation_app.set_owner_autopilot_enabled(
                actor=owner,
                enabled=True,
                now=datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc),
            )
        assert AutomationPolicyRepository(conn).autopilot_enabled_projection(
            actor=owner,
            now="2026-09-12T11:01:00+00:00",
        ) is True
        settings_app.set_business_event_followups_enabled(
            actor=owner,
            enabled=True,
            now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
        )
        policy = AutomationPolicyRepository(conn).effective(
            actor=owner,
            now="2026-09-12T12:01:00+00:00",
        )
        assert policy is not None
        assert policy.spec.mode.value == "autopilot"
        assert "growth.read_only_analysis" in policy.spec.allowed_actions
        assert "events.commercial_followup" in policy.spec.allowed_actions
    conn.close()



def test_legacy_growth_projection_survives_followup_enable(monkeypatch) -> None:
    conn, owner = _owner_db()
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)
    stamp = "2026-09-12T10:00:00+00:00"
    conn.execute(
        "INSERT INTO business_admin_settings("
        "business_id,setting_key,setting_value,updated_by_member_id,created_at,updated_at"
        ") VALUES(?,'autopilot_enabled','true',?,?,?)",
        (owner.business_id, owner.membership_id, stamp, stamp),
    )
    conn.commit()
    repo = AutomationPolicyRepository(conn)
    assert repo.autopilot_enabled_projection(actor=owner, now=stamp) is True
    with patch.object(settings_app, "get_db", side_effect=lambda: _shared(conn)):
        settings_app.set_business_event_followups_enabled(
            actor=owner, enabled=True,
            now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
        )
    policy = repo.effective(actor=owner, now="2026-09-12T12:01:00+00:00")
    assert policy is not None
    assert policy.spec.mode.value == "autopilot"
    assert {"growth.read_only_analysis", "events.commercial_followup"}.issubset(policy.spec.allowed_actions)
    assert repo.autopilot_enabled_projection(actor=owner, now="2026-09-12T12:01:00+00:00") is True
    conn.close()


def test_growth_toggle_preserves_enabled_event_autosend(monkeypatch) -> None:
    conn, owner = _owner_db()
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)
    from clientplatform.application import automation_policy as automation_app
    with patch.object(settings_app, "get_db", side_effect=lambda: _shared(conn)):
        settings_app.set_business_event_followups_enabled(
            actor=owner, enabled=True,
            now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
        )
    with (
        patch.object(automation_app, "get_db", side_effect=lambda: _shared(conn)),
        patch.object(automation_app, "get_db_ro", side_effect=lambda: _shared(conn)),
    ):
        automation_app.set_owner_autopilot_enabled(
            actor=owner, enabled=True,
            now=datetime(2026, 9, 12, 12, 5, tzinfo=timezone.utc),
        )
        policy = AutomationPolicyRepository(conn).effective(
            actor=owner, now="2026-09-12T12:06:00+00:00"
        )
        assert policy is not None and "events.commercial_followup" in policy.spec.allowed_actions
        automation_app.set_owner_autopilot_enabled(
            actor=owner, enabled=False,
            now=datetime(2026, 9, 12, 12, 7, tzinfo=timezone.utc),
        )
    policy = AutomationPolicyRepository(conn).effective(
        actor=owner, now="2026-09-12T12:08:00+00:00"
    )
    assert policy is not None
    assert "events.commercial_followup" in policy.spec.allowed_actions
    assert "growth.read_only_analysis" not in policy.spec.allowed_actions
    stored = EventFollowupSettingsRepository(conn).get(business_id=owner.business_id)
    assert stored is not None and stored.enabled is True
    conn.close()

def test_stored_preference_remains_visible_during_platform_kill_switch(monkeypatch) -> None:
    conn, owner = _owner_db()
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)
    with patch.object(settings_app, "get_db", side_effect=lambda: _shared(conn)):
        settings_app.set_business_event_followups_enabled(
            actor=owner,
            enabled=True,
            now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
        )
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "false")
    stored = EventFollowupSettingsRepository(conn).get(business_id=owner.business_id)
    assert stored is not None and stored.enabled is True
    assert event_followups_enabled_in_conn(conn, business_id=owner.business_id) is False
    with patch.object(settings_app, "get_db_ro", side_effect=lambda: _shared(conn)):
        visible = settings_app.get_business_event_followup_settings(actor=owner)
    assert visible is not None and visible.enabled is True
    conn.close()


def test_policy_source_is_read_after_business_lock(monkeypatch) -> None:
    conn, owner = _owner_db()
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)
    original_effective = AutomationPolicyRepository.effective
    lock_seen = {"value": False}

    def locked_effective(self, *, actor, now=None):
        assert lock_seen["value"] is True
        return original_effective(self, actor=actor, now=now)

    original_lock = AutomationPolicyRepository._lock_business

    def tracking_lock(self, business_id):
        lock_seen["value"] = True
        return original_lock(self, business_id)

    with (
        patch.object(settings_app, "get_db", side_effect=lambda: _shared(conn)),
        patch.object(AutomationPolicyRepository, "_lock_business", new=tracking_lock),
        patch.object(AutomationPolicyRepository, "effective", new=locked_effective),
    ):
        settings_app.set_business_event_followups_enabled(
            actor=owner,
            enabled=True,
            now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
        )
    assert lock_seen["value"] is True
    conn.close()


def test_settings_repository_validation_and_platform_gate(monkeypatch) -> None:
    conn, owner = _owner_db()
    repository = EventFollowupSettingsRepository(conn)
    with unittest.TestCase().assertRaisesRegex(ValueError, "enabled must be boolean"):
        repository.set_enabled(
            business_id=owner.business_id,
            enabled=1,  # type: ignore[arg-type]
            updated_by_member_id=owner.membership_id,
        )
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "unexpected")
    assert event_followups_platform_enabled() is False
    monkeypatch.setenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", "yes")
    assert event_followups_platform_enabled() is True
    conn.close()


def test_event_strategy_flags_are_durable_and_require_nonempty_active_strategy(monkeypatch) -> None:
    conn, owner = _owner_db()
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)
    with patch.object(settings_app, "get_db", side_effect=lambda: _shared(conn)):
        for segment in ("no_show", "join_signal_unpaid", "offer_clicked_unpaid"):
            settings_app.set_business_event_followup_segment_enabled(
                actor=owner, segment=segment, enabled=False,
                now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
            )
        for channel in ("max", "vk"):
            settings_app.set_business_event_followup_channel_enabled(
                actor=owner, channel=channel, enabled=False,
                now=datetime(2026, 9, 12, 12, 1, tzinfo=timezone.utc),
            )
        settings_app.set_business_event_followups_enabled(
            actor=owner, enabled=True,
            now=datetime(2026, 9, 12, 12, 2, tzinfo=timezone.utc),
        )
        stored = EventFollowupSettingsRepository(conn).get(business_id=owner.business_id)
        assert stored is not None
        assert stored.enabled_segments == ("attended_unpaid",)
        assert stored.enabled_channels == ("email",)
        policy = AutomationPolicyRepository(conn).effective(
            actor=owner, now="2026-09-12T12:02:30+00:00"
        )
        assert policy is not None
        event_scope = next(
            scope for scope in policy.spec.action_scopes
            if scope.action == "events.commercial_followup"
        )
        assert event_scope.allowed_channels == ("email",)
        assert policy.spec.allowed_channels == ()

        settings_app.set_business_event_followup_channel_enabled(
            actor=owner, channel="max", enabled=True,
            now=datetime(2026, 9, 12, 12, 2, 30, tzinfo=timezone.utc),
        )
        policy = AutomationPolicyRepository(conn).effective(
            actor=owner, now="2026-09-12T12:02:31+00:00"
        )
        assert policy is not None
        event_scope = next(
            scope for scope in policy.spec.action_scopes
            if scope.action == "events.commercial_followup"
        )
        assert event_scope.allowed_channels == ("email", "max")
        assert policy.spec.allowed_channels == ()

        settings_app.set_business_event_followup_channel_enabled(
            actor=owner, channel="max", enabled=False,
            now=datetime(2026, 9, 12, 12, 2, 45, tzinfo=timezone.utc),
        )
        policy = AutomationPolicyRepository(conn).effective(
            actor=owner, now="2026-09-12T12:02:46+00:00"
        )
        assert policy is not None
        event_scope = next(
            scope for scope in policy.spec.action_scopes
            if scope.action == "events.commercial_followup"
        )
        assert event_scope.allowed_channels == ("email",)
        with unittest.TestCase().assertRaisesRegex(ValueError, "хотя бы одна группа"):
            settings_app.set_business_event_followup_segment_enabled(
                actor=owner, segment="attended_unpaid", enabled=False,
                now=datetime(2026, 9, 12, 12, 3, tzinfo=timezone.utc),
            )
        with unittest.TestCase().assertRaisesRegex(ValueError, "хотя бы один канал"):
            settings_app.set_business_event_followup_channel_enabled(
                actor=owner, channel="email", enabled=False,
                now=datetime(2026, 9, 12, 12, 4, tzinfo=timezone.utc),
            )
    conn.close()


def test_disabling_channel_cancels_pending_work_immediately(monkeypatch) -> None:
    conn, owner = _owner_db()
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)
    with patch.object(settings_app, "get_db", side_effect=lambda: _shared(conn)):
        settings_app.set_business_event_followups_enabled(
            actor=owner, enabled=True,
            now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
        )
        _seed_pending_email_followup(
            conn, owner, registration_id="reg-channel", dispatch_id="dispatch-channel", no_show=True
        )
        settings_app.set_business_event_followup_channel_enabled(
            actor=owner, channel="email", enabled=False,
            now=datetime(2026, 9, 12, 12, 1, tzinfo=timezone.utc),
        )
    row = conn.execute(
        "SELECT status,last_error FROM provider_dispatch_outbox WHERE id='dispatch-channel'"
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("cancelled", "event_commercial_strategy_disabled")
    conn.close()


def test_disabling_segment_cancels_matching_pending_work_immediately(monkeypatch) -> None:
    conn, owner = _owner_db()
    monkeypatch.delenv("CLIENTPLATFORM_EVENT_COMMERCIAL_FOLLOWUPS_ENABLED", raising=False)
    with patch.object(settings_app, "get_db", side_effect=lambda: _shared(conn)):
        settings_app.set_business_event_followups_enabled(
            actor=owner, enabled=True,
            now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
        )
        _seed_pending_email_followup(
            conn, owner, registration_id="reg-segment", dispatch_id="dispatch-segment", no_show=True
        )
        settings_app.set_business_event_followup_segment_enabled(
            actor=owner, segment="no_show", enabled=False,
            now=datetime(2026, 9, 12, 12, 1, tzinfo=timezone.utc),
        )
    row = conn.execute(
        "SELECT status,last_error FROM provider_dispatch_outbox WHERE id='dispatch-segment'"
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("cancelled", "event_commercial_strategy_disabled")
    conn.close()


def test_existing_followup_settings_schema_is_grown_in_place() -> None:
    from services.db.schema import clientplatform_events

    conn, owner = _owner_db()
    conn.execute("DROP TABLE clientplatform_event_followup_settings")
    conn.execute(
        """
        CREATE TABLE clientplatform_event_followup_settings(
            business_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,
            settings_epoch INTEGER NOT NULL DEFAULT 1,updated_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO clientplatform_event_followup_settings VALUES(?,?,?,?,?,?)",
        (owner.business_id, 1, 4, owner.membership_id, "2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
    )
    clientplatform_events.ensure(conn)
    columns = {
        row["name"] for row in conn.execute(
            "PRAGMA table_info(clientplatform_event_followup_settings)"
        ).fetchall()
    }
    assert {
        "segment_no_show", "segment_join_signal", "segment_attended",
        "segment_offer_clicked", "channel_email", "channel_max", "channel_vk",
    }.issubset(columns)
    stored = EventFollowupSettingsRepository(conn).get(business_id=owner.business_id)
    assert stored is not None and stored.enabled is True and stored.settings_epoch == 4
    assert len(stored.enabled_segments) == 4
    assert stored.enabled_channels == ("email", "max", "vk")
    conn.close()
