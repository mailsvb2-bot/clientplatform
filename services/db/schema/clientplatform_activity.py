from __future__ import annotations

import sqlite3

from services.schema_core import _add_col, _cols


def ensure(c: sqlite3.Connection) -> None:
    """Create the extensible business activity, offerings and customer invite boundary."""
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_profiles(
            business_id TEXT PRIMARY KEY,
            activity_description TEXT NOT NULL,
            timezone TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            profile_details_json TEXT NOT NULL DEFAULT '{}',
            profile_confirmed_at TEXT,
            brand_display_name TEXT,
            brand_tone_json TEXT,
            brand_visual_keywords_json TEXT,
            brand_forbidden_visuals_json TEXT,
            brand_primary_color TEXT,
            brand_accent_color TEXT,
            brand_text_color TEXT,
            brand_updated_at TEXT,
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(status IN ('draft', 'ready'))
        )
        """
    )
    # Additive migration for profiles created before structured onboarding and
    # Visual Creative Studio. Structured details stay in the canonical profile
    # row instead of introducing a second business-memory table.
    have_profile = _cols(c, "business_profiles")
    for column, ddl in {
        "profile_details_json": "profile_details_json TEXT NOT NULL DEFAULT '{}'",
        "profile_confirmed_at": "profile_confirmed_at TEXT",
        "brand_display_name": "brand_display_name TEXT",
        "brand_tone_json": "brand_tone_json TEXT",
        "brand_visual_keywords_json": "brand_visual_keywords_json TEXT",
        "brand_forbidden_visuals_json": "brand_forbidden_visuals_json TEXT",
        "brand_primary_color": "brand_primary_color TEXT",
        "brand_accent_color": "brand_accent_color TEXT",
        "brand_text_color": "brand_text_color TEXT",
        "brand_updated_at": "brand_updated_at TEXT",
    }.items():
        if column not in have_profile:
            _add_col(c, "business_profiles", ddl)

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_capabilities(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            connector_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, connector_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(kind IN ('programs', 'consultations', 'services', 'custom')),
            CHECK(status IN ('active', 'disabled'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_offerings(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            capability_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            UNIQUE(id, business_id),
            FOREIGN KEY(capability_id, business_id)
                REFERENCES business_capabilities(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(status IN ('active', 'archived'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_invites(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            expires_at TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            claimed_customer_id TEXT,
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            revoked_at TEXT,
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            FOREIGN KEY(claimed_customer_id, business_id)
                REFERENCES customers(id, business_id),
            CHECK(status IN ('active', 'claimed', 'revoked', 'expired'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_capabilities_active
        ON business_capabilities(business_id, status, connector_key)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_offerings_active
        ON business_offerings(business_id, capability_id, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_customer_invites_business_status
        ON customer_invites(business_id, status, expires_at)
        """
    )
