from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create additive tenant-scoped partner-preparation tables."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS partner_campaigns(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            name TEXT NOT NULL,
            goal_json TEXT NOT NULL,
            automation_mode TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(automation_mode IN ('cautious','normal','autopilot')),
            CHECK(status IN ('active','paused','completed','cancelled'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_partner_campaigns_business_status
        ON partner_campaigns(business_id, status, created_at)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS partner_candidates(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            name TEXT NOT NULL,
            source_url TEXT NOT NULL DEFAULT '',
            audience_summary TEXT NOT NULL DEFAULT '',
            recent_topic TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL,
            contact_value TEXT NOT NULL DEFAULT '',
            contact_basis TEXT NOT NULL DEFAULT 'unknown',
            follower_count INTEGER NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            competitor INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'discovered',
            fit_total REAL NOT NULL DEFAULT 0,
            fit_json TEXT NOT NULL DEFAULT '{}',
            referral_token TEXT NOT NULL UNIQUE,
            discovered_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id, campaign_id),
            UNIQUE(business_id, campaign_id, source_fingerprint),
            FOREIGN KEY(campaign_id, business_id)
                REFERENCES partner_campaigns(id, business_id) ON DELETE CASCADE,
            CHECK(channel IN ('email','telegram','vk','website_form','manual')),
            CHECK(contact_basis IN (
                'public_business_contact','existing_relationship','opted_in','unknown','none'
            )),
            CHECK(status IN (
                'discovered','ready','contacted','replied','accepted','declined',
                'paid_only','do_not_contact','invalid'
            )),
            CHECK(competitor IN (0,1))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_partner_candidates_campaign_rank
        ON partner_candidates(business_id, campaign_id, status, fit_total DESC)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS partner_content_packs(
            candidate_id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            outreach_message TEXT NOT NULL,
            ready_post TEXT NOT NULL,
            followup_message TEXT NOT NULL,
            collaboration_angle TEXT NOT NULL,
            cta TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(candidate_id, business_id),
            FOREIGN KEY(candidate_id, business_id, campaign_id)
                REFERENCES partner_candidates(id, business_id, campaign_id)
                ON DELETE CASCADE
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS partner_placements(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            external_url TEXT NOT NULL DEFAULT '',
            scheduled_at TEXT NULL,
            published_at TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            FOREIGN KEY(candidate_id, business_id, campaign_id)
                REFERENCES partner_candidates(id, business_id, campaign_id)
                ON DELETE CASCADE,
            CHECK(kind IN ('post','joint_live','guest_article','newsletter','other'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_partner_placements_campaign
        ON partner_placements(business_id, campaign_id, published_at)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS partner_referral_events(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            referral_token TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_key TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, dedupe_key),
            FOREIGN KEY(candidate_id, business_id, campaign_id)
                REFERENCES partner_candidates(id, business_id, campaign_id)
                ON DELETE CASCADE,
            CHECK(event_type IN ('opened','result'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_partner_referral_events_campaign
        ON partner_referral_events(business_id, campaign_id, event_type, occurred_at)
        """
    )
