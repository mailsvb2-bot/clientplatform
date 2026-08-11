from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Persist one replaceable media asset per tenant-scoped ad publication."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_publication_assets(
            publication_job_id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            content_type TEXT NOT NULL,
            original_name TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes BIGINT NOT NULL,
            duration_seconds INTEGER,
            provider_image_hash TEXT,
            provider_video_id TEXT,
            provider_creative_id TEXT,
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
            CHECK(size_bytes > 0 AND size_bytes <= 100000000),
            CHECK(duration_seconds IS NULL OR duration_seconds > 0)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_publication_assets_business
        ON ad_publication_assets(business_id, updated_at, publication_job_id)
        """
    )
