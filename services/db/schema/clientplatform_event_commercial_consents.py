from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Persist auditable commercial consent without weakening event registration consent.

    The event log is append-only evidence. The channel-state table is the current
    send authority used at queue time and again at the provider boundary.
    """

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_commercial_consent_events(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            registration_id TEXT NOT NULL,
            action TEXT NOT NULL,
            advertiser_label TEXT NOT NULL,
            consent_text_version TEXT,
            consent_text_sha256 TEXT,
            channels_csv TEXT NOT NULL,
            registration_identity_hash TEXT NOT NULL,
            source TEXT,
            campaign_ref TEXT,
            occurred_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            FOREIGN KEY(registration_id, business_id, event_id)
                REFERENCES clientplatform_event_registrations(id, business_id, event_id)
                ON DELETE CASCADE,
            CHECK(action IN ('grant','revoke')),
            CHECK(length(advertiser_label) BETWEEN 1 AND 240),
            CHECK(length(channels_csv) BETWEEN 1 AND 80),
            CHECK(length(registration_identity_hash)=64),
            CHECK(
                (action='grant'
                    AND consent_text_version IS NOT NULL
                    AND length(consent_text_version) BETWEEN 1 AND 120
                    AND consent_text_sha256 IS NOT NULL
                    AND length(consent_text_sha256)=64)
                OR
                (action='revoke'
                    AND consent_text_version IS NULL
                    AND consent_text_sha256 IS NULL)
            )
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_commercial_consent_events_registration
        ON clientplatform_event_commercial_consent_events(
            business_id,event_id,registration_id,occurred_at,id
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_commercial_channel_state(
            business_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            registration_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            status TEXT NOT NULL,
            grant_event_id TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revoked_at TEXT,
            PRIMARY KEY(business_id,event_id,registration_id,platform),
            FOREIGN KEY(registration_id, business_id, event_id)
                REFERENCES clientplatform_event_registrations(id, business_id, event_id)
                ON DELETE CASCADE,
            FOREIGN KEY(grant_event_id, business_id)
                REFERENCES clientplatform_event_commercial_consent_events(id, business_id)
                ON DELETE CASCADE,
            CHECK(platform IN ('email','vk','max','telegram')),
            CHECK(status IN ('active','revoked')),
            CHECK(
                (status='active' AND revoked_at IS NULL)
                OR (status='revoked' AND revoked_at IS NOT NULL)
            )
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_commercial_channel_active
        ON clientplatform_event_commercial_channel_state(
            business_id,platform,status,event_id,registration_id
        )
        """
    )
