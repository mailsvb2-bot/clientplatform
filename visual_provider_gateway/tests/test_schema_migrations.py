from __future__ import annotations

import sqlite3

from visual_provider_gateway.store import JobStore


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
            """
            CREATE TABLE visual_jobs (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                provider_job_id TEXT NOT NULL,
                model TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                asset_path TEXT NOT NULL,
                error_code TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            INSERT INTO visual_jobs(
                id, provider, kind, status, provider_job_id, model,
                mime_type, asset_path, error_code, created_at, updated_at
            ) VALUES (
                'legacy-job', 'yandexart', 'image', 'succeeded', 'op-1',
                'art://folder/yandex-art/latest', 'image/jpeg', '/data/output/a.jpg',
                '', 1, 2
            );
            """
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
