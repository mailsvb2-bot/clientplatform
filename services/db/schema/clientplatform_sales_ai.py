from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create durable advisory-AI state without granting execution authority."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_sales_ai_heads(
            business_id TEXT NOT NULL,
            lead_id TEXT NOT NULL,
            latest_source_order_key TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(business_id, lead_id),
            FOREIGN KEY(lead_id, business_id)
                REFERENCES clientplatform_sales_leads(id, business_id) ON DELETE CASCADE,
            CHECK(length(latest_source_order_key) = 32)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_sales_ai_jobs(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            lead_id TEXT NOT NULL,
            source_event_dedupe_key TEXT NOT NULL,
            source_order_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            locked_at TEXT,
            lock_token TEXT,
            last_error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            dead_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, lead_id, source_event_dedupe_key),
            FOREIGN KEY(lead_id, business_id)
                REFERENCES clientplatform_sales_leads(id, business_id) ON DELETE CASCADE,
            CHECK(status IN ('pending','processing','retry','done','dead')),
            CHECK(attempts >= 0),
            CHECK(length(source_event_dedupe_key) BETWEEN 1 AND 240),
            CHECK(length(source_order_key) = 32)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_sales_ai_consents(
            business_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            consent_target TEXT NOT NULL DEFAULT '',
            consent_epoch BIGINT NOT NULL DEFAULT 0,
            data_mode TEXT NOT NULL DEFAULT 'redacted',
            customer_notice_confirmed INTEGER NOT NULL DEFAULT 0,
            updated_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(updated_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(enabled IN (0,1)),
            CHECK(customer_notice_confirmed IN (0,1)),
            CHECK(consent_epoch >= 0),
            CHECK(data_mode IN ('redacted','standard','no_cloud'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_sales_ai_analysis_projection(
            business_id TEXT NOT NULL,
            lead_id TEXT NOT NULL,
            source_order_key TEXT NOT NULL,
            source_event_dedupe_key TEXT NOT NULL,
            analysis_json TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            plan_id TEXT,
            action_kind TEXT,
            verified_offer_json TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(business_id, lead_id),
            FOREIGN KEY(lead_id, business_id)
                REFERENCES clientplatform_sales_leads(id, business_id) ON DELETE CASCADE,
            CHECK(length(source_order_key) = 32),
            CHECK(length(source_event_dedupe_key) BETWEEN 1 AND 240)
        )
        """
    )

    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cp_sales_ai_jobs_due
        ON clientplatform_sales_ai_jobs(status, available_at, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cp_sales_ai_jobs_business
        ON clientplatform_sales_ai_jobs(business_id, status, updated_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cp_sales_ai_jobs_lead_order
        ON clientplatform_sales_ai_jobs(business_id, lead_id, source_order_key)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cp_sales_ai_projection_updated
        ON clientplatform_sales_ai_analysis_projection(business_id, updated_at)
        """
    )
