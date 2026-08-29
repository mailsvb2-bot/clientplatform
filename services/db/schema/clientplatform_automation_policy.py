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
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_automation_action_approvals(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            candidate_json TEXT NOT NULL,
            candidate_hash TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            policy_version INTEGER NOT NULL,
            policy_hash TEXT NOT NULL,
            approval_reasons_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_by_member_id TEXT NOT NULL,
            decided_by_member_id TEXT,
            requested_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            decided_at TEXT,
            revoked_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, idempotency_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(policy_id, business_id)
                REFERENCES clientplatform_automation_policies(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(requested_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            FOREIGN KEY(decided_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 200),
            CHECK(length(request_fingerprint) = 64),
            CHECK(length(candidate_hash) = 64),
            CHECK(policy_version > 0),
            CHECK(length(policy_hash) = 64),
            CHECK(length(approval_reasons_json) >= 4),
            CHECK(status IN ('pending', 'approved', 'rejected', 'revoked')),
            CHECK(
                (status='pending' AND decided_by_member_id IS NULL AND decided_at IS NULL AND revoked_at IS NULL)
                OR (status IN ('approved', 'rejected') AND decided_by_member_id IS NOT NULL AND decided_at IS NOT NULL AND revoked_at IS NULL)
                OR (status='revoked' AND decided_by_member_id IS NOT NULL AND decided_at IS NOT NULL AND revoked_at IS NOT NULL)
            )
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_automation_approval_pending
        ON clientplatform_automation_action_approvals(business_id, status, expires_at, requested_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_automation_approval_policy
        ON clientplatform_automation_action_approvals(business_id, policy_id, status)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_automation_approval_candidate
        ON clientplatform_automation_action_approvals(business_id, candidate_hash)
        """
    )
