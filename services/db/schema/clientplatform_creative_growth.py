from __future__ import annotations

import sqlite3

from services.schema_core import _add_col, _cols


def ensure(c: sqlite3.Connection) -> None:
    """Persist tenant-owned creative traffic plans above publication-job variants."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS creative_growth_trials(
            id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            revision INTEGER NOT NULL DEFAULT 1,
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(id),
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(length(name) BETWEEN 1 AND 160),
            CHECK(status IN ('draft','running','paused','completed')),
            CHECK(revision >= 1)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_creative_growth_trials_business
        ON creative_growth_trials(business_id, status, updated_at)
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS creative_growth_trial_variants(
            trial_id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            publication_job_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            allocation_bps INTEGER NOT NULL,
            promotion_source_token TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(trial_id, variant_id),
            UNIQUE(trial_id, publication_job_id),
            UNIQUE(trial_id, position),
            FOREIGN KEY(trial_id, business_id)
                REFERENCES creative_growth_trials(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(publication_job_id, business_id)
                REFERENCES creative_variant_bindings(publication_job_id, business_id)
                ON DELETE CASCADE,
            CHECK(length(variant_id) BETWEEN 1 AND 200),
            CHECK(position BETWEEN 0 AND 7),
            CHECK(allocation_bps BETWEEN 1 AND 10000)
        )
        """
    )
    if "promotion_source_token" not in _cols(c, "creative_growth_trial_variants"):
        _add_col(c, "creative_growth_trial_variants", "promotion_source_token TEXT")
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_creative_growth_trial_variants_business
        ON creative_growth_trial_variants(business_id, trial_id, publication_job_id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_creative_growth_trial_variants_source
        ON creative_growth_trial_variants(business_id, promotion_source_token)
        """
    )
