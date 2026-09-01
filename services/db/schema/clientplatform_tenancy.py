from __future__ import annotations

import sqlite3


BUSINESS_ROLES = (
    "owner",
    "administrator",
    "manager",
    "content_manager",
    "marketer",
    "analyst",
    "support",
)


def ensure(c: sqlite3.Connection) -> None:
    """Create the additive clientplatform tenant boundary without mutating legacy tables."""
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS businesses(
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(status IN ('active', 'suspended', 'archived'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_members(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revoked_at TEXT,
            UNIQUE(business_id, user_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            CHECK(role IN (
                'owner', 'administrator', 'manager', 'content_manager',
                'marketer', 'analyst', 'support'
            )),
            CHECK(status IN ('active', 'revoked'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_owner_control_workspaces(
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            business_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, platform),
            FOREIGN KEY(business_id, user_id)
                REFERENCES business_members(business_id, user_id) ON DELETE CASCADE,
            CHECK(platform IN ('telegram', 'vk', 'max'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_owner_control_workspace_business
        ON clientplatform_owner_control_workspaces(business_id, platform)
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_owner_onboarding_sessions(
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            step TEXT NOT NULL,
            business_id TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, platform),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            CHECK(platform IN ('telegram', 'vk', 'max')),
            CHECK(step IN ('business_name', 'activity_description')),
            CHECK(
                (step='business_name' AND business_id IS NULL)
                OR (step='activity_description' AND business_id IS NOT NULL)
            )
        )
        """
    )
    c.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_business_members_id_business
        ON business_members(id, business_id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_members_user_status
        ON business_members(user_id, status)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_members_business_status
        ON business_members(business_id, status)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_businesses_creator_status
        ON businesses(created_by_user_id, status)
        """
    )
