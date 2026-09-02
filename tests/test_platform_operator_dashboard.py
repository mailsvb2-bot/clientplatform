from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services import platform_operator_dashboard as dashboard
from services.platform_resource_limits import PlatformResourceSnapshot, ResourceCounter


class _Recovery:
    def to_dict(self) -> dict[str, object]:
        return {
            "status": "GREEN",
            "reason": "restore_target_configured",
            "backup_count": 2,
        }


def _must_not_run(*args, **kwargs):
    raise AssertionError("protected source must not run")


def test_platform_operator_snapshot_denies_before_reading_sources(monkeypatch):
    monkeypatch.setattr(dashboard, "is_platform_admin", lambda _user_id: False)
    monkeypatch.setattr(dashboard, "disaster_recovery_status", _must_not_run)
    monkeypatch.setattr(dashboard, "format_runtime_contract_report", _must_not_run)
    monkeypatch.setattr(dashboard, "get_platform_resource_snapshot", _must_not_run)

    with pytest.raises(
        dashboard.PlatformOperatorPermissionDenied,
        match="platform operator access required",
    ):
        dashboard.platform_operator_snapshot(9001)


def test_platform_operator_snapshot_is_platform_scoped_and_probe_free_by_default(
    monkeypatch,
):
    monkeypatch.setattr(dashboard, "is_platform_admin", lambda _user_id: True)
    monkeypatch.setattr(
        dashboard,
        "disaster_recovery_status",
        lambda *, include_hash: _Recovery(),
    )
    monkeypatch.setattr(
        dashboard,
        "format_runtime_contract_report",
        lambda: "runtime contract green",
    )
    monkeypatch.setattr(dashboard, "get_platform_resource_snapshot", _must_not_run)

    snapshot = dashboard.platform_operator_snapshot(
        9001,
        now_utc=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
    )

    assert snapshot["scope"] == "platform"
    assert snapshot["business_data_included"] is False
    assert snapshot["generated_at_utc"] == "2026-09-02T12:00:00+00:00"
    assert snapshot["release_contract"] == {"report": "runtime contract green"}
    assert snapshot["disaster_recovery"]["status"] == "GREEN"
    assert snapshot["resource_telemetry"] == {
        "requested": False,
        "status": "NOT_REQUESTED",
        "snapshot": None,
    }


def test_platform_operator_snapshot_can_opt_in_to_resource_telemetry(monkeypatch):
    monkeypatch.setattr(dashboard, "is_platform_admin", lambda _user_id: True)
    monkeypatch.setattr(
        dashboard,
        "disaster_recovery_status",
        lambda *, include_hash: _Recovery(),
    )
    monkeypatch.setattr(
        dashboard,
        "format_runtime_contract_report",
        lambda: "runtime contract green",
    )
    monkeypatch.setattr(
        dashboard,
        "get_platform_resource_snapshot",
        lambda: PlatformResourceSnapshot(
            configured=True,
            telemetry_available=True,
            base_url="https://visual.example.test",
            token_configured=True,
            day_utc="2026-09-02",
            resets_at="2026-09-03T00:00:00Z",
            usage_semantics="reservations",
            jobs=ResourceCounter(used=10, limit=100, remaining=90),
            image=ResourceCounter(used=4, limit=50, remaining=46),
            video=ResourceCounter(used=2, limit=20, remaining=18),
            active=ResourceCounter(used=1, limit=5, remaining=4),
        ),
    )

    snapshot = dashboard.platform_operator_snapshot(
        9001,
        include_resource_telemetry=True,
        now_utc=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
    )

    telemetry = snapshot["resource_telemetry"]
    assert telemetry["requested"] is True
    assert telemetry["status"] == "AVAILABLE"
    assert telemetry["snapshot"]["jobs"] == {
        "used": 10,
        "limit": 100,
        "remaining": 90,
    }


def test_platform_operator_snapshot_rejects_naive_clock(monkeypatch):
    monkeypatch.setattr(dashboard, "is_platform_admin", lambda _user_id: True)
    monkeypatch.setattr(dashboard, "disaster_recovery_status", _must_not_run)

    with pytest.raises(ValueError, match="now_utc must be timezone-aware"):
        dashboard.platform_operator_snapshot(
            9001,
            now_utc=datetime(2026, 9, 2, 12, 0),
        )
