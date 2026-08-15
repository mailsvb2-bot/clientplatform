from __future__ import annotations

import sqlite3


_OUTCOME_TYPES_SQL = """
    'booking_created',
    'payment_received',
    'order_created',
    'lead_created',
    'manual_review_completed',
    'outcome_correction',
    'outcome_reversal'
"""


def ensure(c: sqlite3.Connection) -> None:
    """Create the append-only, tenant-scoped business outcome ledger."""
    c.execute(
        f"""
        CREATE TABLE IF NOT EXISTS business_outcome_events(
            event_id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            customer_id TEXT,
            outcome_type TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            subject_ref TEXT,
            occurred_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            amount_minor INTEGER,
            currency TEXT,
            metadata_json TEXT NOT NULL,
            metadata_version INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL,
            PRIMARY KEY(business_id, event_id),
            UNIQUE(business_id, idempotency_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id),
            CHECK(outcome_type IN ({_OUTCOME_TYPES_SQL})),
            CHECK(length(trim(source_type)) > 0),
            CHECK(length(trim(source_id)) > 0),
            CHECK(length(trim(idempotency_key)) > 0),
            CHECK(metadata_version >= 1),
            CHECK(
                (amount_minor IS NULL AND currency IS NULL)
                OR
                (amount_minor IS NOT NULL AND currency IS NOT NULL)
            ),
            CHECK(currency IS NULL OR (length(currency) = 3 AND currency = upper(currency))),
            CHECK(outcome_type != 'payment_received' OR amount_minor IS NOT NULL)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outcomes_business_occurred
        ON business_outcome_events(business_id, occurred_at, event_id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outcomes_business_type_occurred
        ON business_outcome_events(business_id, outcome_type, occurred_at, event_id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outcomes_business_source_occurred
        ON business_outcome_events(business_id, source_type, source_id, occurred_at, event_id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outcomes_business_customer_occurred
        ON business_outcome_events(business_id, customer_id, occurred_at, event_id)
        """
    )
