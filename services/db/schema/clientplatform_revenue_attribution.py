from __future__ import annotations

import sqlite3


def ensure(c: sqlite3.Connection) -> None:
    """Create durable, tenant-scoped assignments from monetary outcomes to acquisition touches."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS revenue_attributions(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            outcome_event_id TEXT NOT NULL,
            outcome_type TEXT NOT NULL,
            customer_id TEXT,
            touch_id TEXT NOT NULL,
            attribution_identity_id TEXT NOT NULL,
            source TEXT NOT NULL,
            source_ref_type TEXT NOT NULL,
            source_ref_id TEXT NOT NULL,
            promotion_campaign_id TEXT,
            model_version TEXT NOT NULL,
            amount_minor BIGINT NOT NULL,
            currency TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, outcome_event_id, model_version),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(outcome_event_id, business_id)
                REFERENCES business_outcome_events(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(customer_id, business_id)
                REFERENCES customers(id, business_id) ON DELETE SET NULL,
            FOREIGN KEY(touch_id, business_id)
                REFERENCES acquisition_touches(id, business_id) ON DELETE RESTRICT,
            FOREIGN KEY(attribution_identity_id, business_id)
                REFERENCES attribution_identities(id, business_id) ON DELETE RESTRICT,
            FOREIGN KEY(promotion_campaign_id, business_id)
                REFERENCES promotion_campaigns(id, business_id) ON DELETE SET NULL,
            CHECK(outcome_type IN ('order_paid', 'refund_recorded', 'outcome_reversal')),
            CHECK(source IN (
                'organic','referral','telegram','vk','max','website',
                'yandex_direct','partner','manual_import','unknown'
            )),
            CHECK(model_version='first_touch_v1'),
            CHECK(length(currency)=3 AND currency=upper(currency)),
            CHECK(length(trim(source_ref_type)) > 0),
            CHECK(length(trim(source_ref_id)) > 0)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_revenue_attributions_business_time
        ON revenue_attributions(business_id, occurred_at, outcome_event_id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_revenue_attributions_business_source_time
        ON revenue_attributions(business_id, source, occurred_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_revenue_attributions_campaign_time
        ON revenue_attributions(business_id, promotion_campaign_id, occurred_at)
        """
    )
