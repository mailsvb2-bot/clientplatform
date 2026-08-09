from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create the canonical provider dispatch outbox.

    The historical ``delivery_dispatch_outbox`` is retained as a rollback/read
    compatibility source, but all new runtime work is materialized into this
    generic outbox. Lesson deliveries and partner outreach therefore share one
    lease/idempotency/transport contour instead of running parallel senders.
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
            CHECK(source_kind IN ('lesson_delivery', 'partner_outreach')),
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
                    AND source_id=logical_delivery_id)
                OR
                (source_kind='partner_outreach'
                    AND logical_delivery_id IS NULL
                    AND partner_campaign_id IS NOT NULL
                    AND partner_candidate_id IS NOT NULL
                    AND source_id=partner_candidate_id)
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

    # One-way idempotent adoption of already-materialized lesson work. The old
    # table remains untouched so a binary rollback can still inspect it, while
    # the new runtime has one canonical queue to claim from.
    c.execute(
        """
        INSERT INTO provider_dispatch_outbox(
            id, business_id, platform, source_kind, source_id,
            logical_delivery_id, partner_campaign_id, partner_candidate_id,
            connection_id, recipient_kind, customer_identity_id,
            external_subject, payload_kind, payload_ref, idempotency_key,
            status, attempts, available_at, locked_at, lock_token,
            provider_message_id, last_error, created_at, updated_at,
            sent_at, dead_at
        )
        SELECT
            d.id, d.business_id, d.platform, 'lesson_delivery',
            d.logical_delivery_id, d.logical_delivery_id, NULL, NULL,
            d.connection_id, 'customer_identity', d.customer_identity_id,
            ci.external_subject, d.payload_kind, d.payload_ref,
            d.idempotency_key, d.status, d.attempts, d.available_at,
            d.locked_at, d.lock_token, d.provider_message_id, d.last_error,
            d.created_at, d.updated_at, d.sent_at, d.dead_at
        FROM delivery_dispatch_outbox d
        JOIN customer_identities ci
          ON ci.id=d.customer_identity_id
         AND ci.business_id=d.business_id
         AND ci.platform=d.platform
        WHERE NOT EXISTS (
            SELECT 1 FROM provider_dispatch_outbox p
            WHERE p.business_id=d.business_id
              AND p.idempotency_key=d.idempotency_key
        )
        """
    )
