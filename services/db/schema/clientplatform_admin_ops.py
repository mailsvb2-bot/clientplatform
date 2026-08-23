from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create tenant-safe ClientPlatform admin operations and observability tables."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_admin_settings(
            business_id TEXT NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL,
            updated_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(business_id, setting_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(updated_by_member_id, business_id)
                REFERENCES business_members(id, business_id)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_publications(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            scheduled_at TEXT,
            published_at TEXT,
            failed_at TEXT,
            failure_reason TEXT,
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(channel IN ('telegram', 'vk', 'max', 'other')),
            CHECK(status IN ('draft', 'scheduled', 'published', 'failed', 'cancelled'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_payments(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            customer_id TEXT,
            amount_minor BIGINT NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'paid',
            provider TEXT NOT NULL DEFAULT 'manual',
            external_reference TEXT,
            note TEXT NOT NULL DEFAULT '',
            recorded_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            paid_at TEXT,
            refunded_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, provider, external_reference),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(customer_id, business_id)
                REFERENCES customers(id, business_id),
            FOREIGN KEY(recorded_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(amount_minor > 0),
            CHECK(status IN ('pending', 'paid', 'failed', 'refunded', 'cancelled'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_offering_prices(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            offering_id TEXT NOT NULL,
            amount_minor BIGINT NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, offering_id),
            FOREIGN KEY(offering_id, business_id)
                REFERENCES business_offerings(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(amount_minor > 0),
            CHECK(status IN ('active', 'archived'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_payment_outcome_evidence(
            business_id TEXT NOT NULL,
            payment_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            outcome_event_id TEXT NOT NULL,
            offering_id TEXT,
            provider TEXT NOT NULL,
            external_reference TEXT,
            recorded_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(business_id, idempotency_key),
            UNIQUE(business_id, payment_id, operation),
            UNIQUE(business_id, outcome_event_id),
            UNIQUE(business_id, operation, provider, external_reference),
            FOREIGN KEY(payment_id, business_id)
                REFERENCES business_payments(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(business_id, outcome_event_id)
                REFERENCES business_outcome_events(business_id, id) ON DELETE RESTRICT,
            FOREIGN KEY(offering_id, business_id)
                REFERENCES business_offerings(id, business_id),
            FOREIGN KEY(recorded_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(operation IN ('paid', 'refund')),
            CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 200),
            CHECK(length(request_fingerprint) = 64),
            CHECK(length(trim(provider)) BETWEEN 1 AND 40)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_subscription_state(
            business_id TEXT PRIMARY KEY,
            plan_key TEXT NOT NULL,
            status TEXT NOT NULL,
            included_staff INTEGER NOT NULL,
            included_customers INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            renews_at TEXT,
            updated_by_member_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(updated_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(included_staff >= 1),
            CHECK(included_customers >= 0),
            CHECK(status IN ('trial', 'active', 'past_due', 'suspended', 'cancelled'))
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_admin_audit_events(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            actor_user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_admin_interaction_metrics(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            actor_user_id INTEGER NOT NULL,
            callback_action TEXT NOT NULL,
            success INTEGER NOT NULL,
            ack_ms INTEGER NOT NULL,
            lock_wait_ms INTEGER NOT NULL,
            app_ms INTEGER NOT NULL,
            telegram_ms INTEGER NOT NULL,
            total_ms INTEGER NOT NULL,
            transport_role TEXT NOT NULL,
            transport_route TEXT NOT NULL,
            transport_generation INTEGER,
            error_code TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            CHECK(success IN (0, 1)),
            CHECK(ack_ms >= 0),
            CHECK(lock_wait_ms >= 0),
            CHECK(app_ms >= 0),
            CHECK(telegram_ms >= 0),
            CHECK(total_ms >= 0)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_admin_alerts(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            severity TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            occurrences INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            resolved_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, kind),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            CHECK(severity IN ('warning', 'critical')),
            CHECK(status IN ('open', 'resolved')),
            CHECK(occurrences > 0)
        )
        """
    )

    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_publications_status
        ON business_publications(business_id, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_payments_status
        ON business_payments(business_id, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_prices_status
        ON business_offering_prices(business_id, status, updated_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_payment_evidence_payment
        ON business_payment_outcome_evidence(business_id, payment_id, operation)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_admin_audit_recent
        ON clientplatform_admin_audit_events(business_id, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_admin_metrics_recent
        ON clientplatform_admin_interaction_metrics(business_id, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_admin_alerts_open
        ON clientplatform_admin_alerts(business_id, status, severity, last_seen_at)
        """
    )
