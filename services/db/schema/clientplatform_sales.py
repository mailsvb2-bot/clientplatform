from __future__ import annotations

import sqlite3

from services.schema_core import _add_col, _cols


def ensure(c: sqlite3.Connection) -> None:
    """Create the canonical tenant-scoped sales opportunity boundary."""

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_sales_leads(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            opportunity_key TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            offering_id TEXT,
            source_kind TEXT NOT NULL,
            source_ref TEXT,
            contact_basis TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'new',
            assigned_member_id TEXT,
            next_action TEXT,
            due_at TEXT,
            closure_reason TEXT,
            last_signal_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, opportunity_key),
            FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
            FOREIGN KEY(customer_id, business_id)
                REFERENCES customers(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(offering_id, business_id)
                REFERENCES business_offerings(id, business_id),
            FOREIGN KEY(assigned_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(contact_basis IN (
                'inbound','explicit_consent','existing_customer','requested_followup','none'
            )),
            CHECK(stage IN ('new','contacted','qualified','checkout','won','lost'))
        )
        """
    )
    have_leads = _cols(c, "clientplatform_sales_leads")
    for column, ddl in {
        "next_action": "next_action TEXT",
        "due_at": "due_at TEXT",
        "closure_reason": "closure_reason TEXT",
    }.items():
        if column not in have_leads:
            _add_col(c, "clientplatform_sales_leads", ddl)

    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cp_sales_leads_business_stage
        ON clientplatform_sales_leads(business_id, stage, updated_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cp_sales_leads_customer
        ON clientplatform_sales_leads(business_id, customer_id, updated_at)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cp_sales_leads_due
        ON clientplatform_sales_leads(business_id, due_at, updated_at)
        WHERE due_at IS NOT NULL AND stage NOT IN ('won','lost')
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_sales_events(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            lead_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            UNIQUE(business_id, lead_id, dedupe_key),
            FOREIGN KEY(lead_id, business_id)
                REFERENCES clientplatform_sales_leads(id, business_id) ON DELETE CASCADE
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cp_sales_events_lead
        ON clientplatform_sales_events(business_id, lead_id, occurred_at, id)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_sales_action_plans(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            lead_id TEXT NOT NULL,
            action_kind TEXT NOT NULL,
            rationale TEXT NOT NULL,
            requires_approval INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'planned',
            created_by_member_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(id, business_id),
            FOREIGN KEY(lead_id, business_id)
                REFERENCES clientplatform_sales_leads(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(action_kind IN (
                'respond','ask_qualification','present_offer',
                'checkout_followup','human_handoff','noop'
            )),
            CHECK(requires_approval IN (0, 1)),
            CHECK(status IN ('planned','approved','dismissed','executed'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cp_sales_plans_business_status
        ON clientplatform_sales_action_plans(business_id, status, created_at)
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_sales_conversation_state(
            lead_id TEXT NOT NULL,
            business_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'discovered',
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(lead_id, business_id),
            FOREIGN KEY(lead_id, business_id)
                REFERENCES clientplatform_sales_leads(id, business_id) ON DELETE CASCADE,
            CHECK(state IN (
                'discovered','engaged','need_known','qualified',
                'offer_presented','checkout','won','lost','handoff'
            )),
            CHECK(version >= 1)
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS clientplatform_sales_handoffs(
            id TEXT PRIMARY KEY,
            business_id TEXT NOT NULL,
            lead_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            severity TEXT NOT NULL,
            summary TEXT NOT NULL,
            context_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'open',
            created_by_member_id TEXT NOT NULL,
            claimed_by_member_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            UNIQUE(id, business_id),
            FOREIGN KEY(lead_id, business_id)
                REFERENCES clientplatform_sales_leads(id, business_id) ON DELETE CASCADE,
            FOREIGN KEY(created_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            FOREIGN KEY(claimed_by_member_id, business_id)
                REFERENCES business_members(id, business_id),
            CHECK(reason IN (
                'explicit_request','low_confidence','sensitive_context',
                'pricing_exception','negative_sentiment','repeated_failure'
            )),
            CHECK(severity IN ('normal','high','urgent')),
            CHECK(status IN ('open','claimed','resolved'))
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cp_sales_handoffs_queue
        ON clientplatform_sales_handoffs(business_id, status, severity, created_at)
        """
    )
    c.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_cp_sales_handoffs_active_lead
        ON clientplatform_sales_handoffs(business_id, lead_id)
        WHERE status IN ('open','claimed')
        """
    )
