from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Persist rebuildable mechanics for one canonical business offering."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_offering_processes(
            offering_id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            revision INTEGER NOT NULL DEFAULT 1,
            mechanics_json TEXT NOT NULL DEFAULT '{}',
            ai_profile_json TEXT NOT NULL DEFAULT '{}',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            frozen_at TEXT,
            purge_after TEXT,
            purged_at TEXT,
            PRIMARY KEY(offering_id, business_id),
            FOREIGN KEY(offering_id, business_id)
                REFERENCES business_offerings(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(state IN ('active','frozen','purged')),
            CHECK(revision >= 1),
            CHECK(
                (state='active' AND frozen_at IS NULL AND purge_after IS NULL AND purged_at IS NULL)
                OR (state='frozen' AND frozen_at IS NOT NULL AND purge_after IS NOT NULL AND purged_at IS NULL)
                OR (state='purged' AND frozen_at IS NOT NULL AND purge_after IS NOT NULL AND purged_at IS NOT NULL)
            )
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_offering_processes_retention
        ON business_offering_processes(state, purge_after, business_id, offering_id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_offering_processes_business
        ON business_offering_processes(business_id, state, updated_at)
        """
    )
