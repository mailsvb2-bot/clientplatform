from __future__ import annotations

import sqlite3
from pathlib import Path

from visual_provider_gateway.store import JobStore

_FIXTURES = Path(__file__).with_name("fixtures")


def test_fresh_provider_store_applies_explicit_schema_assets(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    JobStore(str(path))

    with sqlite3.connect(path) as conn:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(visual_jobs)").fetchall()
        }
        indexes = {
            str(row[1])
            for row in conn.execute("PRAGMA index_list(visual_jobs)").fetchall()
        }

    assert {
        "client_id",
        "scope_id",
        "idempotency_key",
        "request_fingerprint",
    } <= columns
    assert "ux_visual_jobs_client_scope_idempotency" in indexes
    assert "ix_visual_jobs_client_created" in indexes


def test_recovered_legacy_provider_database_is_forward_migrated_without_data_loss(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            (_FIXTURES / "legacy_visual_jobs.sql").read_text(encoding="utf-8")
        )

    JobStore(str(path))

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM visual_jobs WHERE id='legacy-job'").fetchone()
        columns = {
            str(item[1])
            for item in conn.execute("PRAGMA table_info(visual_jobs)").fetchall()
        }

    assert row is not None
    assert row["provider"] == "yandexart"
    assert row["client_id"] == "legacy"
    assert row["scope_id"] == "global"
    assert row["idempotency_key"] == ""
    assert row["request_fingerprint"] == ""
    assert {
        "client_id",
        "scope_id",
        "idempotency_key",
        "request_fingerprint",
    } <= columns
