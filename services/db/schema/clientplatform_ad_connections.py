from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create provider-neutral advertising account, consent and outbox boundaries."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_connections(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            external_account_id TEXT NOT NULL,
            external_login TEXT NOT NULL,
            identity_source TEXT NOT NULL DEFAULT 'direct_client_id',
            credential_ciphertext TEXT NOT NULL,
            permissions_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_success_at TEXT,
            last_error_at TEXT,
            last_error_code TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, provider, external_account_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(provider IN ('yandex_direct')),
            CHECK(identity_source IN ('legacy_oauth', 'direct_client_id')),
            CHECK(status IN ('pending', 'active', 'attention', 'disabled', 'revoked'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_oauth_sessions(
            state_hash TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            user_id BIGINT NOT NULL,
            membership_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            verifier_ciphertext TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(membership_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(provider IN ('yandex_direct'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_publication_jobs(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            promotion_campaign_id TEXT NOT NULL,
            connection_id TEXT NOT NULL,
            external_campaign_id TEXT NOT NULL,
            external_campaign_name TEXT NOT NULL,
            region_ids_json TEXT NOT NULL,
            source_url TEXT NOT NULL,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            idempotency_key TEXT NOT NULL,
            external_ad_group_id TEXT,
            external_ad_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            locked_at TEXT,
            lock_token TEXT,
            last_error_code TEXT,
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            submitted_at TEXT,
            dead_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, idempotency_key),
            FOREIGN KEY(promotion_campaign_id, business_id)
                REFERENCES promotion_campaigns(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(connection_id, business_id)
                REFERENCES ad_connections(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(status IN (
                'draft', 'queued', 'publishing', 'retry',
                'submitted', 'failed', 'cancelled'
            )),
            CHECK(attempts >= 0)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_spend_authorizations(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            connection_id TEXT NOT NULL,
            publication_job_id TEXT NOT NULL,
            external_campaign_id TEXT NOT NULL,
            region_ids_json TEXT NOT NULL,
            currency TEXT NOT NULL,
            hard_cap_minor BIGINT NOT NULL,
            daily_cap_minor BIGINT NOT NULL,
            authorization_expires_at TEXT NOT NULL,
            stop_condition TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            request_key TEXT NOT NULL,
            consent_receipt_id TEXT,
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revoked_at TEXT,
            stopped_at TEXT,
            last_error_code TEXT,
            row_version BIGINT NOT NULL DEFAULT 0,
            UNIQUE(id, business_id),
            UNIQUE(business_id, request_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(connection_id, business_id)
                REFERENCES ad_connections(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(publication_job_id, business_id)
                REFERENCES ad_publication_jobs(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(length(currency)=3),
            CHECK(hard_cap_minor > 0),
            CHECK(daily_cap_minor > 0 AND daily_cap_minor <= hard_cap_minor),
            CHECK(row_version >= 0),
            CHECK(stop_condition IN ('hard_cap_or_daily_cap_or_expiry')),
            CHECK(status IN (
                'draft', 'awaiting_consent', 'authorized', 'launching', 'active',
                'stopping', 'stopped', 'expired', 'revoked', 'failed'
            ))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_spend_consent_receipts(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            authorization_id TEXT NOT NULL,
            actor_member_id TEXT NOT NULL,
            actor_user_id BIGINT NOT NULL,
            terms_json TEXT NOT NULL,
            terms_hash TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL,
            consented_at TEXT NOT NULL,
            receipt_hash TEXT NOT NULL,
            version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, authorization_id),
            UNIQUE(receipt_hash),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(authorization_id, business_id)
                REFERENCES ad_spend_authorizations(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(actor_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(actor_user_id > 0),
            CHECK(version='1')
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_audit_events(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            actor_member_id TEXT NOT NULL,
            action TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(actor_member_id, business_id)
                REFERENCES business_members(id, business_id)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_connections_business_status
        ON ad_connections(business_id, provider, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_oauth_sessions_expiry
        ON ad_oauth_sessions(expires_at, consumed_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_publication_jobs_due
        ON ad_publication_jobs(status, available_at, locked_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_publication_jobs_business
        ON ad_publication_jobs(business_id, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_spend_authorizations_business_status
        ON ad_spend_authorizations(business_id, status, authorization_expires_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_spend_authorizations_snapshot
        ON ad_spend_authorizations(business_id, snapshot_hash, status)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_spend_receipts_business_time
        ON ad_spend_consent_receipts(business_id, consented_at, authorization_id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ad_audit_events_business
        ON ad_audit_events(business_id, created_at, action)
        """
    )