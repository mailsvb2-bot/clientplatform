from __future__ import annotations

import sqlite3


_OUTCOME_TYPES_SQL = """
    'lead_created',
    'lead_qualified',
    'booking_created',
    'booking_confirmed',
    'booking_completed',
    'order_paid',
    'customer_reactivated',
    'refund_recorded',
    'outcome_correction',
    'outcome_reversal'
"""


def ensure(c: sqlite3.Connection) -> None:
    """Create the append-only, tenant-scoped business outcome ledger."""
    c.execute(
        f"""
        CREATE TABLE IF NOT EXISTS business_outcome_events(
            id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            outcome_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            customer_id TEXT,
            subject_ref TEXT,
            amount_minor INTEGER,
            currency TEXT,
            idempotency_key TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            metadata_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(business_id, id),
            UNIQUE(business_id, idempotency_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id),
            FOREIGN KEY(customer_id, business_id) REFERENCES customers(id, business_id),
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
            CHECK(outcome_type NOT IN ('order_paid', 'refund_recorded') OR amount_minor IS NOT NULL)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outcomes_business_occurred
        ON business_outcome_events(business_id, occurred_at, id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outcomes_business_type_occurred
        ON business_outcome_events(business_id, outcome_type, occurred_at, id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outcomes_business_source_occurred
        ON business_outcome_events(business_id, source_type, source_id, occurred_at, id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outcomes_business_customer_occurred
        ON business_outcome_events(business_id, customer_id, occurred_at, id)
        """
    )
