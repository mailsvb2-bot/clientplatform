from __future__ import annotations

import sqlite3

from clientplatform.domain.creative_generation import (
    MAX_CREATIVE_GENERATION_PROVIDER_PAYLOAD_CHARS,
)


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
        f"""
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
            CHECK(length(provider_payload_json) BETWEEN 1 AND {MAX_CREATIVE_GENERATION_PROVIDER_PAYLOAD_CHARS}),
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

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS visual_scene_plan_receipts(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            plan_key TEXT NOT NULL,
            request_text TEXT NOT NULL,
            style_json TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, created_by_member_id, plan_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id) ON DELETE CASCADE,
            CHECK(length(plan_key)=64),
            CHECK(length(request_text) BETWEEN 1 AND 1500),
            CHECK(length(style_json) BETWEEN 2 AND 2500),
            CHECK(length(result_json) <= 12000),
            CHECK(status IN ('planning','ready','ambiguous'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_visual_scene_plan_business_updated
        ON visual_scene_plan_receipts(business_id, created_by_member_id, updated_at)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS editable_ad_projects(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            publication_job_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            headline TEXT NOT NULL,
            body TEXT NOT NULL,
            cta TEXT NOT NULL DEFAULT '',
            layout TEXT NOT NULL DEFAULT 'lower_card',
            font_preset TEXT NOT NULL DEFAULT 'auto',
            brand_json TEXT NOT NULL,
            source_job_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, created_by_member_id, publication_job_id, kind),
            FOREIGN KEY(publication_job_id, business_id)
                REFERENCES ad_publication_jobs(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(kind IN ('image','video')),
            CHECK(length(headline) BETWEEN 1 AND 160),
            CHECK(length(body) BETWEEN 1 AND 500),
            CHECK(length(cta) <= 80),
            CHECK(layout IN ('lower_card','top_card')),
            CHECK(font_preset IN (
                'auto','modern','strict','friendly','premium',
                'editorial','elegant','bold_ad'
            )),
            CHECK(length(brand_json) BETWEEN 2 AND 512),
            CHECK(length(source_job_id) <= 128),
            CHECK(status IN ('draft','source_ready','source_expired','finished')),
            CHECK(revision >= 1)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_editable_ad_projects_publication
        ON editable_ad_projects(business_id, publication_job_id, kind, updated_at)
        """
    )


    editable_columns = {
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in c.execute("PRAGMA table_info(editable_ad_projects)").fetchall()
    }
    if "font_preset" not in editable_columns:
        c.execute(
            "ALTER TABLE editable_ad_projects "
            "ADD COLUMN font_preset TEXT NOT NULL DEFAULT 'auto' "
            "CHECK(font_preset IN ("
            "'auto','modern','strict','friendly','premium',"
            "'editorial','elegant','bold_ad'"
            "))"
        )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS visual_style_preferences(
            business_id TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            style_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(business_id, created_by_member_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id) ON DELETE CASCADE,
            CHECK(length(style_json) BETWEEN 2 AND 2000)
        )
        """
    )
