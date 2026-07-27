from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import probe_auto_audio_load_dry_run as load_probe
from services.probe_safety import ProbeMutationAuthorizationRequired


def test_load_probe_rejects_unauthorized_mutation_before_db_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []
    monkeypatch.setattr(load_probe, "init_db", lambda: touched.append("init_db"))

    with pytest.raises(ProbeMutationAuthorizationRequired):
        load_probe.run_load_probe(
            users=1,
            concurrency=1,
            slot="morning",
            allow_live_db_mutation=False,
        )

    assert touched == []


def test_load_probe_forwards_explicit_mutation_authorization_to_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialized: list[bool] = []
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(load_probe, "init_db", lambda: initialized.append(True))

    def fake_run_probe(**kwargs: object) -> SimpleNamespace:
        calls.append(dict(kwargs))
        return SimpleNamespace(rows_touched=4)

    monkeypatch.setattr(load_probe, "run_probe", fake_run_probe)

    result = load_probe.run_load_probe(
        users=3,
        concurrency=2,
        slot="evening",
        allow_live_db_mutation=True,
    )

    assert initialized == [True]
    assert result["ok"] is True
    assert result["users"] == 3
    assert result["rows_touched"] == 12
    assert len(calls) == 3
    assert all(call["allow_live_db_mutation"] is True for call in calls)
    assert all(call["initialize_schema"] is False for call in calls)
    assert all(call["keep_artifacts"] is False for call in calls)
    assert all(call["slot"] == "evening" for call in calls)


def test_main_accepts_scoped_production_gate_authorization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_load_probe(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True, "probe": "auto_audio_load_dry_run"}

    monkeypatch.setattr(load_probe, "run_load_probe", fake_load_probe)
    monkeypatch.setenv("METRO_PROBE_ALLOW_LIVE_DB_MUTATION", "1")
    monkeypatch.setattr(
        load_probe.sys,
        "argv",
        ["probe_auto_audio_load_dry_run.py", "--users", "2", "--concurrency", "1"],
    )

    assert load_probe.main() == 0
    assert captured["allow_live_db_mutation"] is True
    assert captured["users"] == 2
    assert '"ok": true' in capsys.readouterr().out


def test_main_fails_closed_without_authorization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("METRO_PROBE_ALLOW_LIVE_DB_MUTATION", raising=False)
    monkeypatch.setattr(load_probe.sys, "argv", ["probe_auto_audio_load_dry_run.py"])
    monkeypatch.setattr(
        load_probe,
        "run_load_probe",
        lambda **_kwargs: (_ for _ in ()).throw(
            ProbeMutationAuthorizationRequired("probe_mutation_authorization_required")
        ),
    )

    assert load_probe.main() == 2
    output = capsys.readouterr().out
    assert '"database_touched": false' in output
    assert "probe_mutation_authorization_required" in output
