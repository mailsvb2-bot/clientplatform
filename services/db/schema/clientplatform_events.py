from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create provider-neutral online-event storage.

    The webinar/meeting provider is deliberately data (provider_key + HTTPS
    join_url), not a database enum. New providers therefore do not require a
    schema migration.
    """

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_events(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            created_by_member_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            starts_at TEXT NOT NULL,
            ends_at TEXT,
            timezone_name TEXT NOT NULL,
            provider_key TEXT NOT NULL,
            provider_label TEXT,
            join_url TEXT NOT NULL,
            offer_url TEXT,
            public_slug TEXT NOT NULL UNIQUE,
            consent_version TEXT NOT NULL,
            notification_connection_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            FOREIGN KEY(notification_connection_id, business_id)
                REFERENCES connections(id, business_id),
            CHECK(kind IN ('webinar','online_event','workshop','masterclass')),
            CHECK(status IN ('draft','published','cancelled','completed')),
            CHECK(length(title) BETWEEN 1 AND 180),
            CHECK(length(provider_key) BETWEEN 1 AND 64),
            CHECK(length(public_slug) BETWEEN 20 AND 96)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_events_business_start
        ON clientplatform_events(business_id, starts_at, id)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_registrations(
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            customer_id TEXT,
            status TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            identity_hash TEXT NOT NULL,
            source TEXT,
            campaign_ref TEXT,
            token TEXT NOT NULL UNIQUE,
            consent_version TEXT NOT NULL,
            consented_at TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            first_join_click_at TEXT,
            attendance_confirmed_at TEXT,
            attendance_source TEXT,
            offer_clicked_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(id, business_id, event_id),
            UNIQUE(event_id, identity_hash),
            FOREIGN KEY(event_id, business_id)
                REFERENCES clientplatform_events(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(customer_id)
                REFERENCES customers(id) ON DELETE SET NULL,
            CHECK(status IN ('registered','cancelled')),
            CHECK(length(name) BETWEEN 1 AND 120),
            CHECK(length(email) BETWEEN 3 AND 320),
            CHECK(length(identity_hash)=64),
            CHECK(length(token) BETWEEN 32 AND 128)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_event_reg_business
        ON clientplatform_event_registrations(business_id, event_id, registered_at, id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_event_reg_customer
        ON clientplatform_event_registrations(business_id, customer_id)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_conversion_links(
            business_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            registration_id TEXT NOT NULL,
            payment_ref TEXT NOT NULL,
            amount_minor INTEGER,
            currency TEXT,
            occurred_at TEXT NOT NULL,
            PRIMARY KEY(business_id, event_id, registration_id, payment_ref),
            FOREIGN KEY(event_id, business_id)
                REFERENCES clientplatform_events(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(registration_id, business_id, event_id)
                REFERENCES clientplatform_event_registrations(id, business_id, event_id)
                ON DELETE CASCADE,
            CHECK(amount_minor IS NULL OR amount_minor >= 0),
            CHECK(currency IS NULL OR length(currency)=3),
            CHECK(length(payment_ref) BETWEEN 1 AND 512)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_clientplatform_event_conversion_event
        ON clientplatform_event_conversion_links(business_id, event_id, occurred_at)
        """
    )
