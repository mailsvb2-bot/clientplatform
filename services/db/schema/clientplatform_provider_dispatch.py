from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create generic provider work for non-lesson ClientPlatform sends.

    Lesson delivery remains in ``delivery_dispatch_outbox`` during this rollout.
    The canonical worker leases both stores and uses the same credential,
    adapter, retry and idempotency machinery. This deliberately avoids copying
    in-flight lesson work while an older process may still own its lease.
    """

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_dispatch_outbox(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            logical_delivery_id TEXT NULL,
            partner_campaign_id TEXT NULL,
            partner_candidate_id TEXT NULL,
            sales_followup_id TEXT NULL,
            connection_id TEXT NOT NULL,
            recipient_kind TEXT NOT NULL,
            customer_identity_id TEXT NULL,
            external_subject TEXT NOT NULL,
            payload_kind TEXT NOT NULL,
            payload_ref TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            locked_at TEXT,
            lock_token TEXT,
            provider_message_id TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            sent_at TEXT,
            dead_at TEXT,
            UNIQUE(id, business_id),
            UNIQUE(business_id, idempotency_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(connection_id, business_id, platform)
                REFERENCES connections(id, business_id, platform),
            FOREIGN KEY(logical_delivery_id, business_id)
                REFERENCES lesson_deliveries(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(customer_identity_id, business_id, platform)
                REFERENCES customer_identities(id, business_id, platform),
            FOREIGN KEY(partner_candidate_id, business_id, partner_campaign_id)
                REFERENCES partner_candidates(id, business_id, campaign_id)
                ON DELETE CASCADE,
            CHECK(platform IN ('telegram', 'vk', 'max')),
            CHECK(source_kind IN ('lesson_delivery', 'partner_outreach', 'sales_followup', 'customer_interaction', 'member_interaction')),
            CHECK(recipient_kind IN ('customer_identity', 'external_subject')),
            CHECK(payload_kind IN (
                'audio', 'video', 'text', 'document', 'image',
                'link', 'task', 'mixed'
            )),
            CHECK(status IN (
                'pending', 'sending', 'retry', 'sent', 'dead', 'cancelled'
            )),
            CHECK(attempts >= 0),
            CHECK(length(external_subject) > 0),
            CHECK(
                (source_kind='lesson_delivery'
                    AND logical_delivery_id IS NOT NULL
                    AND partner_campaign_id IS NULL
                    AND partner_candidate_id IS NULL
                    AND sales_followup_id IS NULL
                    AND source_id=logical_delivery_id)
                OR
                (source_kind='partner_outreach'
                    AND logical_delivery_id IS NULL
                    AND partner_campaign_id IS NOT NULL
                    AND partner_candidate_id IS NOT NULL
                    AND sales_followup_id IS NULL
                    AND source_id=partner_candidate_id)
                OR
                (source_kind='sales_followup'
                    AND logical_delivery_id IS NULL
                    AND partner_campaign_id IS NULL
                    AND partner_candidate_id IS NULL
                    AND sales_followup_id IS NOT NULL
                    AND source_id=sales_followup_id)
                OR
                (source_kind IN ('customer_interaction','member_interaction')
                    AND logical_delivery_id IS NULL
                    AND partner_campaign_id IS NULL
                    AND partner_candidate_id IS NULL
                    AND sales_followup_id IS NULL)
            ),
            CHECK(
                (recipient_kind='customer_identity'
                    AND customer_identity_id IS NOT NULL)
                OR
                (recipient_kind='external_subject'
                    AND customer_identity_id IS NULL)
            )
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_provider_dispatch_due
        ON provider_dispatch_outbox(status, available_at, locked_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_provider_dispatch_business_source
        ON provider_dispatch_outbox(business_id, source_kind, source_id, status)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_provider_dispatch_partner_reply
        ON provider_dispatch_outbox(
            business_id, platform, connection_id, external_subject,
            source_kind, status, sent_at
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS partner_reply_events(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            connection_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            external_subject TEXT NOT NULL,
            provider_event_key TEXT NOT NULL,
            reply_text TEXT NOT NULL DEFAULT '',
            occurred_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, connection_id, provider_event_key),
            FOREIGN KEY(candidate_id, business_id, campaign_id)
                REFERENCES partner_candidates(id, business_id, campaign_id)
                ON DELETE CASCADE,
            FOREIGN KEY(connection_id, business_id, platform)
                REFERENCES connections(id, business_id, platform),
            CHECK(platform IN ('telegram', 'vk', 'max')),
            CHECK(length(external_subject) > 0),
            CHECK(length(provider_event_key) > 0)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_partner_reply_events_candidate
        ON partner_reply_events(business_id, campaign_id, candidate_id, occurred_at)
        """
    )
