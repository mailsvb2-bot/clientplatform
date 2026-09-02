from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create the additive platform-support capability and immutable audit contour."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_platform_support_sessions(
            id TEXT PRIMARY KEY,
            operator_user_id INTEGER NOT NULL,
            business_id TEXT NOT NULL,
            ticket_ref TEXT NOT NULL,
            reason TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            revoked_by_user_id INTEGER,
            UNIQUE(operator_user_id, idempotency_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE RESTRICT,
            CHECK(status IN ('active', 'revoked')),
            CHECK(operator_user_id > 0),
            CHECK(expires_at > issued_at),
            CHECK(revoked_by_user_id IS NULL OR revoked_by_user_id > 0),
            CHECK(length(trim(ticket_ref)) BETWEEN 1 AND 160),
            CHECK(length(trim(reason)) BETWEEN 1 AND 500),
            CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 200),
            CHECK(length(request_fingerprint) = 64),
            CHECK(
                (status='active' AND revoked_at IS NULL AND revoked_by_user_id IS NULL)
                OR
                (status='revoked' AND revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)
            )
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_platform_support_sessions_business
        ON clientplatform_platform_support_sessions(business_id, status, expires_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_platform_support_sessions_operator
        ON clientplatform_platform_support_sessions(operator_user_id, issued_at)
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_platform_support_audit_events(
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            operator_user_id INTEGER NOT NULL,
            business_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id)
                REFERENCES clientplatform_platform_support_sessions(id) ON DELETE RESTRICT,
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE RESTRICT,
            CHECK(event_type IN ('issued', 'session_read', 'business_metadata_read', 'revoked'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_platform_support_audit_business
        ON clientplatform_platform_support_audit_events(business_id, created_at, event_type)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_platform_support_audit_session
        ON clientplatform_platform_support_audit_events(session_id, created_at)
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_platform_operator_audit_events(
            id TEXT PRIMARY KEY,
            operator_user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            query_kind TEXT NOT NULL,
            query_fingerprint TEXT NOT NULL,
            result_count INTEGER NOT NULL,
            result_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(operator_user_id > 0),
            CHECK(action='directory_lookup'),
            CHECK(query_kind IN ('business_id','user_id','business_name')),
            CHECK(length(query_fingerprint) = 64),
            CHECK(result_count BETWEEN 0 AND 20),
            CHECK(length(result_fingerprint) = 64)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_platform_operator_audit_recent
        ON clientplatform_platform_operator_audit_events(operator_user_id, created_at, action)
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_support_cases(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            category TEXT NOT NULL,
            summary TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_by_member_id TEXT NOT NULL,
            claimed_by_operator_user_id INTEGER,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            claimed_at TEXT,
            resolved_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, created_by_member_id, idempotency_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE RESTRICT,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id) ON DELETE RESTRICT,
            CHECK(category IN ('general','billing','technical','security','integration')),
            CHECK(status IN ('open','claimed','resolved')),
            CHECK(length(trim(summary)) BETWEEN 3 AND 1000),
            CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 200),
            CHECK(length(request_fingerprint) = 64),
            CHECK(claimed_by_operator_user_id IS NULL OR claimed_by_operator_user_id > 0),
            CHECK(
                (status='open' AND claimed_by_operator_user_id IS NULL
                    AND claimed_at IS NULL AND resolved_at IS NULL)
                OR (status='claimed' AND claimed_by_operator_user_id IS NOT NULL
                    AND claimed_at IS NOT NULL AND resolved_at IS NULL)
                OR (status='resolved' AND claimed_by_operator_user_id IS NOT NULL
                    AND claimed_at IS NOT NULL AND resolved_at IS NOT NULL)
            )
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_support_cases_business
        ON clientplatform_support_cases(business_id, status, created_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_support_cases_platform_queue
        ON clientplatform_support_cases(status, created_at, id)
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_support_case_audit_events(
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor_kind TEXT NOT NULL,
            actor_ref TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id, business_id)
                REFERENCES clientplatform_support_cases(id, business_id) ON DELETE RESTRICT,
            CHECK(event_type IN ('created','claimed','released','resolved')),
            CHECK(actor_kind IN ('tenant_member','platform_operator')),
            CHECK(length(trim(actor_ref)) BETWEEN 1 AND 160)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_support_case_audit_case
        ON clientplatform_support_case_audit_events(case_id, created_at, event_type)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_support_case_audit_business
        ON clientplatform_support_case_audit_events(business_id, created_at, event_type)
        """
    )
