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


    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_channel_link_tokens(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            registration_id TEXT NOT NULL,
            token_digest TEXT NOT NULL UNIQUE,
            target_platform TEXT NOT NULL,
            marketing_requested INTEGER NOT NULL DEFAULT 0,
            consent_text_sha256 TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            consumed_external_subject TEXT,
            UNIQUE(id,business_id),
            FOREIGN KEY(registration_id,business_id,event_id)
                REFERENCES clientplatform_event_registrations(id,business_id,event_id)
                ON DELETE CASCADE,
            CHECK(length(token_digest)=64),
            CHECK(target_platform IN ('telegram','vk','max')),
            CHECK(marketing_requested IN (0,1)),
            CHECK(
                (marketing_requested=0 AND consent_text_sha256 IS NULL)
                OR
                (marketing_requested=1
                    AND consent_text_sha256 IS NOT NULL
                    AND length(consent_text_sha256)=64)
            ),
            CHECK(
                (consumed_at IS NULL AND consumed_external_subject IS NULL)
                OR
                (consumed_at IS NOT NULL
                    AND consumed_external_subject IS NOT NULL
                    AND length(consumed_external_subject) BETWEEN 1 AND 512)
            )
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_channel_link_registration
        ON clientplatform_event_channel_link_tokens(
            business_id,event_id,registration_id,target_platform,expires_at
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_event_registration_channels(
            business_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            registration_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            external_subject TEXT NOT NULL,
            connection_id TEXT,
            verified_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(business_id,event_id,registration_id,platform),
            FOREIGN KEY(registration_id,business_id,event_id)
                REFERENCES clientplatform_event_registrations(id,business_id,event_id)
                ON DELETE CASCADE,
            FOREIGN KEY(connection_id,business_id,platform)
                REFERENCES connections(id,business_id,platform)
                ON DELETE SET NULL,
            CHECK(platform IN ('telegram','vk','max')),
            CHECK(length(external_subject) BETWEEN 1 AND 512)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_registration_channels_recipient
        ON clientplatform_event_registration_channels(
            business_id,platform,external_subject,event_id,registration_id
        )
        """
    )
