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
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_followup_scan_state(
            scope TEXT PRIMARY KEY,
            cursor_starts_at TEXT,
            cursor_event_id TEXT,
            cursor_registration_id TEXT,
            updated_at TEXT NOT NULL,
            CHECK(length(scope) BETWEEN 1 AND 80)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_followup_settings(
            business_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            segment_no_show INTEGER NOT NULL DEFAULT 1,
            segment_join_signal INTEGER NOT NULL DEFAULT 1,
            segment_attended INTEGER NOT NULL DEFAULT 1,
            segment_offer_clicked INTEGER NOT NULL DEFAULT 1,
            channel_email INTEGER NOT NULL DEFAULT 1,
            channel_max INTEGER NOT NULL DEFAULT 1,
            channel_vk INTEGER NOT NULL DEFAULT 1,
            settings_epoch INTEGER NOT NULL DEFAULT 1,
            updated_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(updated_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(enabled IN (0,1)),
            CHECK(segment_no_show IN (0,1)),
            CHECK(segment_join_signal IN (0,1)),
            CHECK(segment_attended IN (0,1)),
            CHECK(segment_offer_clicked IN (0,1)),
            CHECK(channel_email IN (0,1)),
            CHECK(channel_max IN (0,1)),
            CHECK(channel_vk IN (0,1)),
            CHECK(settings_epoch >= 1)
        )
        """
    )
    # Existing production databases already have the master switch table.
    # Grow it in place so strategy settings are available without recreating data.
    followup_columns = {
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in c.execute(
            "PRAGMA table_info(clientplatform_event_followup_settings)"
        ).fetchall()
    }
    for column in (
        "segment_no_show",
        "segment_join_signal",
        "segment_attended",
        "segment_offer_clicked",
        "channel_email",
        "channel_max",
        "channel_vk",
    ):
        if column not in followup_columns:
            c.execute(
                f"ALTER TABLE clientplatform_event_followup_settings "  # nosec B608
                f"ADD COLUMN {column} INTEGER NOT NULL DEFAULT 1"
            )
