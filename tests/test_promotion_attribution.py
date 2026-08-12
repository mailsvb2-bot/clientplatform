from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

from clientplatform.application.promotion_attribution import load_promotion_attribution


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE promotion_events(business_id TEXT, campaign_id TEXT, "
        "customer_id TEXT, event_type TEXT, occurred_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE promotion_campaigns(id TEXT, business_id TEXT, offering_id TEXT, "
        "PRIMARY KEY(id, business_id))"
    )
    conn.execute(
        "CREATE TABLE clientplatform_sales_leads(id TEXT, business_id TEXT, "
        "customer_id TEXT, offering_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE clientplatform_sales_events(business_id TEXT, lead_id TEXT, "
        "event_type TEXT, payload_json TEXT, occurred_at TEXT)"
    )
    return conn


def test_local_attribution_counts_unique_customers_and_exact_won_path() -> None:
    conn = _db()
    business_id = str(uuid4())
    campaign_id = str(uuid4())
    offering_id = str(uuid4())
    customer_id = str(uuid4())
    lead_id = str(uuid4())
    conn.execute(
        "INSERT INTO promotion_campaigns VALUES(?, ?, ?)",
        (campaign_id, business_id, offering_id),
    )
    conn.executemany(
        "INSERT INTO promotion_events VALUES(?, ?, ?, ?, ?)",
        [
            (business_id, campaign_id, customer_id, "opened", "2026-08-10T10:00:00+00:00"),
            (business_id, campaign_id, customer_id, "opened", "2026-08-10T10:01:00+00:00"),
            (business_id, campaign_id, customer_id, "booked", "2026-08-10T10:05:00+00:00"),
        ],
    )
    conn.execute(
        "INSERT INTO clientplatform_sales_leads VALUES(?, ?, ?, ?)",
        (lead_id, business_id, customer_id, offering_id),
    )
    conn.execute(
        "INSERT INTO clientplatform_sales_events VALUES(?, ?, 'conversation_transition', ?, ?)",
        (
            business_id,
            lead_id,
            json.dumps({"from": "checkout", "to": "won"}),
            "2026-08-10T10:20:00+00:00",
        ),
    )

    result = load_promotion_attribution(
        conn,
        business_id=business_id,
        promotion_campaign_ids={campaign_id},
        event_from="2026-08-10T00:00:00+00:00",
        event_until="2026-08-11T00:00:00+00:00",
    )

    assert result.leads[campaign_id] == frozenset({customer_id})
    assert result.bookings[campaign_id] == frozenset({customer_id})
    assert result.won[campaign_id] == frozenset({customer_id})


def test_won_is_not_attributed_without_same_campaign_booking() -> None:
    conn = _db()
    business_id = str(uuid4())
    booked_campaign = str(uuid4())
    other_campaign = str(uuid4())
    offering_id = str(uuid4())
    customer_id = str(uuid4())
    lead_id = str(uuid4())
    conn.executemany(
        "INSERT INTO promotion_campaigns VALUES(?, ?, ?)",
        [
            (booked_campaign, business_id, offering_id),
            (other_campaign, business_id, offering_id),
        ],
    )
    conn.execute(
        "INSERT INTO promotion_events VALUES(?, ?, ?, 'booked', ?)",
        (business_id, booked_campaign, customer_id, "2026-08-10T10:05:00+00:00"),
    )
    conn.execute(
        "INSERT INTO clientplatform_sales_leads VALUES(?, ?, ?, ?)",
        (lead_id, business_id, customer_id, offering_id),
    )
    conn.execute(
        "INSERT INTO clientplatform_sales_events VALUES(?, ?, 'conversation_transition', ?, ?)",
        (
            business_id,
            lead_id,
            json.dumps({"to": "won"}),
            "2026-08-10T10:20:00+00:00",
        ),
    )

    result = load_promotion_attribution(
        conn,
        business_id=business_id,
        promotion_campaign_ids={other_campaign},
        event_from="2026-08-10T00:00:00+00:00",
        event_until="2026-08-11T00:00:00+00:00",
    )

    assert result.won.get(other_campaign, frozenset()) == frozenset()
