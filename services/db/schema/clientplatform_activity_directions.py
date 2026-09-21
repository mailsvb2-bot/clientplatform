from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create generic organization activity directions and polymorphic bindings."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_activity_directions(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, title),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(status IN ('active','archived')),
            CHECK(length(title) BETWEEN 1 AND 160),
            CHECK(length(description) BETWEEN 1 AND 2000)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_activity_direction_bindings(
            business_id TEXT NOT NULL,
            direction_id TEXT NOT NULL,
            subject_kind TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(business_id, subject_kind, subject_id),
            FOREIGN KEY(direction_id, business_id)
                REFERENCES clientplatform_activity_directions(id, business_id)
                ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(subject_kind IN ('program','offering','event'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_activity_directions_business_status
        ON clientplatform_activity_directions(business_id, status, created_at, id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_activity_direction_bindings_direction
        ON clientplatform_activity_direction_bindings(
            business_id, direction_id, subject_kind, created_at
        )
        """
    )


__all__ = ["ensure"]
