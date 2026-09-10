from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Persist the selected creative variant for one tenant-scoped ad draft."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS creative_variant_bindings(
            publication_job_id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            experiment_id TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            angle_id TEXT NOT NULL,
            country_code TEXT NOT NULL DEFAULT '',
            copy_digest TEXT NOT NULL,
            source_job_id TEXT NOT NULL DEFAULT '',
            render_pack_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(publication_job_id, business_id),
            FOREIGN KEY(publication_job_id, business_id)
                REFERENCES ad_publication_jobs(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(status IN ('selected', 'generating', 'rendering', 'attached', 'failed')),
            CHECK(length(experiment_id) BETWEEN 1 AND 200),
            CHECK(length(variant_id) BETWEEN 1 AND 200),
            CHECK(length(angle_id) BETWEEN 1 AND 200),
            CHECK(country_code='' OR length(country_code)=2),
            CHECK(length(copy_digest)=64),
            CHECK(length(source_job_id) <= 128),
            CHECK(length(render_pack_id) <= 128)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_creative_variant_bindings_experiment
        ON creative_variant_bindings(business_id, experiment_id, updated_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_creative_variant_bindings_variant
        ON creative_variant_bindings(business_id, variant_id, updated_at)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS creative_generation_receipts(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            request_text TEXT NOT NULL,
            brand_context TEXT NOT NULL DEFAULT '',
            country_code TEXT NOT NULL DEFAULT '',
            provider_payload_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            source_job_id TEXT NOT NULL DEFAULT '',
            delivery_claimed_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, idempotency_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(length(request_text) BETWEEN 1 AND 1500),
            CHECK(length(brand_context) <= 2500),
            CHECK(country_code='' OR length(country_code)=2),
            CHECK(length(provider_payload_json) BETWEEN 1 AND 10000),
            CHECK(length(idempotency_key) BETWEEN 8 AND 200),
            CHECK(length(source_job_id) <= 128),
            CHECK(status IN (
                'prepared', 'submitting', 'queued', 'running',
                'succeeded', 'failed', 'delivered'
            ))
        )
        """
    )
    columns = {
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in c.execute("PRAGMA table_info(creative_generation_receipts)").fetchall()
    }
    if "delivery_claimed_at" not in columns:
        c.execute(
            "ALTER TABLE creative_generation_receipts "
            "ADD COLUMN delivery_claimed_at TEXT NOT NULL DEFAULT ''"
        )

    c.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_creative_generation_active_member
        ON creative_generation_receipts(business_id, created_by_member_id)
        WHERE status IN ('prepared','submitting','queued','running','succeeded')
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_creative_generation_business_updated
        ON creative_generation_receipts(business_id, updated_at)
        """
    )
