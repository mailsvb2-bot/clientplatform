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
