from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create the append-only, tenant-scoped business outcome ledger."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS business_outcome_events(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            amount_minor INTEGER,
            currency TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT NOT NULL,
            source TEXT NOT NULL,
            correction_of_event_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(business_id, idempotency_key),
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id)
                REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(correction_of_event_id, business_id)
                REFERENCES business_outcome_events(id, business_id),
            CHECK((amount_minor IS NULL AND currency IS NULL) OR
                  (amount_minor IS NOT NULL AND currency IS NOT NULL)),
            CHECK(currency IS NULL OR length(currency) = 3)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_outcomes_type_time
        ON business_outcome_events(business_id, event_type, occurred_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_outcomes_subject_time
        ON business_outcome_events(business_id, subject_type, subject_id, occurred_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_business_outcomes_correction
        ON business_outcome_events(business_id, correction_of_event_id)
        """
    )
