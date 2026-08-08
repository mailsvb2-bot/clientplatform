from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create tenant-scoped commercial ladders without embedding vendor prices."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS commercial_ladders(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, name),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(status IN ('active','archived'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS commercial_ladder_steps(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            ladder_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            offering_id TEXT,
            min_evidence_score REAL NOT NULL DEFAULT 0.0,
            requires_human_approval INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, ladder_id, position),
            FOREIGN KEY(ladder_id, business_id)
                REFERENCES commercial_ladders(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(offering_id, business_id)
                REFERENCES business_offerings(id, business_id),
            CHECK(position >= 0),
            CHECK(kind IN ('diagnostic','audit','implementation','recurring')),
            CHECK(min_evidence_score >= 0.0 AND min_evidence_score <= 1.0),
            CHECK(requires_human_approval IN (0,1))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_commercial_ladders_business_status
        ON commercial_ladders(business_id, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_commercial_ladder_steps_order
        ON commercial_ladder_steps(business_id, ladder_id, position)
        """
    )
