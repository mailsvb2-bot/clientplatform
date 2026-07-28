from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create tenant-safe consultation/service booking slots."""
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_slots(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            offering_id TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            booked_customer_id TEXT,
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            booked_at TEXT,
            cancelled_at TEXT,
            UNIQUE(id, business_id),
            FOREIGN KEY(offering_id, business_id)
                REFERENCES business_offerings(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(booked_customer_id, business_id)
                REFERENCES customers(id, business_id),
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(status IN ('open', 'booked', 'cancelled', 'completed')),
            CHECK(duration_minutes BETWEEN 15 AND 1440),
            CHECK(ends_at > starts_at)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_booking_slots_business_time
        ON booking_slots(business_id, status, starts_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_booking_slots_offering_time
        ON booking_slots(business_id, offering_id, status, starts_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_booking_slots_customer_time
        ON booking_slots(business_id, booked_customer_id, status, starts_at)
        """
    )
