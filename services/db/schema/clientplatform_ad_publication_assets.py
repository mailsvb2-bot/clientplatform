from __future__ import annotations

import sqlite3

from services.db.runtime import is_postgres_enabled


def _column_names(c: sqlite3.Connection) -> set[str]:
    if is_postgres_enabled():
        rows = c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='ad_publication_assets'"
        ).fetchall()
        return {
            str(row["column_name"] if hasattr(row, "keys") else row[0])
            for row in rows
        }
    rows = c.execute("PRAGMA table_info(ad_publication_assets)").fetchall()
    return {
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in rows
    }


def _upgrade_legacy_shape(c: sqlite3.Connection) -> None:
    columns = _column_names(c)
    if not columns:
        return
    if "storage_path" in columns:
        # The owner confirmed there are no production users yet. Preserve only
        # already-provider-backed metadata and eliminate the obsolete local-path
        # column before the product opens to users.
        c.execute(
            "DELETE FROM ad_publication_assets "
            "WHERE (kind='image' AND provider_image_hash IS NULL) "
            "OR (kind='video' AND provider_video_id IS NULL)"
        )
        c.execute("ALTER TABLE ad_publication_assets DROP COLUMN storage_path")
        columns.discard("storage_path")
    if "provider_upload_status" not in columns:
        c.execute(
            "ALTER TABLE ad_publication_assets "
            "ADD COLUMN provider_upload_status TEXT NOT NULL DEFAULT 'ready'"
        )
    if "provider_upload_claim_token" not in columns:
        c.execute(
            "ALTER TABLE ad_publication_assets "
            "ADD COLUMN provider_upload_claim_token TEXT NOT NULL DEFAULT ''"
        )


def ensure(c: sqlite3.Connection) -> None:
    """Persist one replaceable media asset per tenant-scoped ad publication."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_publication_assets(
            publication_job_id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            content_type TEXT NOT NULL,
            original_name TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes BIGINT NOT NULL,
            duration_seconds INTEGER,
            provider_image_hash TEXT,
            provider_video_id TEXT,
            provider_creative_id TEXT,
            provider_error_code TEXT,
            provider_upload_status TEXT NOT NULL DEFAULT 'ready',
            provider_upload_claim_token TEXT NOT NULL DEFAULT '',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(publication_job_id, business_id),
            FOREIGN KEY(publication_job_id, business_id)
                REFERENCES ad_publication_jobs(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(kind IN ('image', 'video')),
            CHECK(source IN ('upload', 'generated')),
            CHECK(provider_upload_status IN ('uploading', 'ready', 'ambiguous', 'failed')),
            CHECK(
                provider_upload_status<>'ready'
                OR (kind='image' AND provider_image_hash IS NOT NULL)
                OR (kind='video' AND provider_video_id IS NOT NULL)
            ),
            CHECK(size_bytes > 0 AND size_bytes <= 100000000),
            CHECK(duration_seconds IS NULL OR duration_seconds > 0)
        )
        """
    )
    _upgrade_legacy_shape(c)
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_publication_assets_business
        ON ad_publication_assets(business_id, updated_at, publication_job_id)
        """
    )
