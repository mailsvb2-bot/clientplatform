from __future__ import annotations

import importlib


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("METRO_DB_PATH", str(tmp_path / "account_audio_backfill.db"))
    monkeypatch.setenv("METRO_DB_ENGINE", "sqlite")

    modules = {}
    for name in [
        "core.paths",
        "services.db.core",
        "services.schema_core",
        "services.schema",
        "services.accounts.identity",
        "services.accounts.audio_progress",
        "services.accounts.audio_backfill",
    ]:
        module = importlib.import_module(name)
        modules[name] = importlib.reload(module)

    modules["services.schema"].init_db()
    return modules


def test_account_audio_backfill_dry_run_uses_highest_legacy_progress(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    backfill = modules["services.accounts.audio_backfill"]
    db_core = importlib.import_module("services.db")

    with db_core.db() as conn:
        conn.execute(
            """
            INSERT INTO user_audio_progress(
                user_id, sequence_key, last_anchor, last_title, last_path, last_platform,
                delivered_at, updated_at, last_confirmed_at,
                pending_anchor, pending_title, pending_path, pending_platform, pending_token, pending_delivered_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """.strip(),
            (
                20002,
                "full_series",
                1,
                "clientplatform",
                "audio/full/clientplatform.opus",
                "max",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        )
        conn.execute(
            """
            INSERT INTO user_audio_progress(
                user_id, sequence_key, last_anchor, last_title, last_path, last_platform,
                delivered_at, updated_at, last_confirmed_at,
                pending_anchor, pending_title, pending_path, pending_platform, pending_token, pending_delivered_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """.strip(),
            (
                30003,
                "full_series",
                2,
                "A2",
                "audio/full/a2.opus",
                "vk",
                "2026-01-02T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        )

    plan = backfill.build_account_audio_progress_backfill_plan(10001, [10001, 20002, 30003])

    assert plan.planned_last_completed_audio_no == 2
    assert plan.planned_last_sent_audio_no == 2
    assert plan.planned_pending_audio_no is None


def test_account_audio_backfill_apply_does_not_downgrade_existing_account_progress(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    audio = modules["services.accounts.audio_progress"]
    backfill = modules["services.accounts.audio_backfill"]
    db_core = importlib.import_module("services.db")

    audio.mark_audio_completed(10001, 5, platform="telegram")

    with db_core.db() as conn:
        conn.execute(
            """
            INSERT INTO user_audio_progress(
                user_id, sequence_key, last_anchor, last_title, last_path, last_platform,
                delivered_at, updated_at, last_confirmed_at,
                pending_anchor, pending_title, pending_path, pending_platform, pending_token, pending_delivered_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """.strip(),
            (
                20002,
                "full_series",
                2,
                "A2",
                "audio/full/a2.opus",
                "vk",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        )

    backfill.apply_account_audio_progress_backfill(10001, [10001, 20002])

    state = audio.get_audio_state(10001)
    assert state.last_completed_audio_no == 5
    assert state.last_sent_audio_no == 5
    assert state.pending_audio_no is None


def test_account_audio_backfill_preserves_future_pending(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    audio = modules["services.accounts.audio_progress"]
    backfill = modules["services.accounts.audio_backfill"]
    db_core = importlib.import_module("services.db")

    audio.mark_audio_completed(10001, 2, platform="telegram")

    with db_core.db() as conn:
        conn.execute(
            """
            INSERT INTO user_audio_progress(
                user_id, sequence_key, last_anchor, last_title, last_path, last_platform,
                delivered_at, updated_at, last_confirmed_at,
                pending_anchor, pending_title, pending_path, pending_platform, pending_token, pending_delivered_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """.strip(),
            (
                20002,
                "full_series",
                2,
                "A2",
                "audio/full/a2.opus",
                "vk",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                3,
                "A3",
                "audio/full/a3.opus",
                "vk",
                None,
                "2026-01-01T00:01:00+00:00",
            ),
        )

    backfill.apply_account_audio_progress_backfill(10001, [10001, 20002])

    state = audio.get_audio_state(10001)
    assert state.last_completed_audio_no == 2
    assert state.last_sent_audio_no == 3
    assert state.pending_audio_no == 3
