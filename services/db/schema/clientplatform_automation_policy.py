from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_automation_policies(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            mode TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            approved_by_member_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT,
            revoked_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, version),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            FOREIGN KEY(approved_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(version > 0),
            CHECK(status IN ('draft', 'approved', 'superseded', 'revoked')),
            CHECK(mode IN ('cautious', 'normal', 'autopilot')),
            CHECK(length(policy_hash) = 64)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_automation_policy_current
        ON clientplatform_automation_policies(business_id, status, version)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_automation_policy_hash
        ON clientplatform_automation_policies(business_id, policy_hash)
        """
    )
    c.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_clientplatform_automation_policy_approved
        ON clientplatform_automation_policies(business_id)
        WHERE status='approved'
        """
    )
