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
        "source_token TEXT, customer_id TEXT, event_type TEXT, occurred_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE promotion_campaigns(id TEXT, business_id TEXT, offering_id TEXT, "
        "source_token TEXT, PRIMARY KEY(id, business_id))"
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
    base_source = "campaignSource12"
    conn.execute(
        "INSERT INTO promotion_campaigns VALUES(?, ?, ?, ?)",
        (campaign_id, business_id, offering_id, base_source),
    )
    conn.executemany(
        "INSERT INTO promotion_events VALUES(?, ?, ?, ?, ?, ?)",
        [
            (business_id, campaign_id, base_source, customer_id, "opened", "2026-08-10T10:00:00+00:00"),
            (business_id, campaign_id, base_source, customer_id, "opened", "2026-08-10T10:01:00+00:00"),
            (business_id, campaign_id, base_source, customer_id, "booked", "2026-08-10T10:05:00+00:00"),
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
    assert result.source_leads[base_source] == frozenset({customer_id})
    assert result.source_bookings[base_source] == frozenset({customer_id})
    assert result.source_won[base_source] == frozenset({customer_id})


def test_won_is_not_attributed_without_same_campaign_booking() -> None:
    conn = _db()
    business_id = str(uuid4())
    booked_campaign = str(uuid4())
    other_campaign = str(uuid4())
    offering_id = str(uuid4())
    customer_id = str(uuid4())
    lead_id = str(uuid4())
    conn.executemany(
        "INSERT INTO promotion_campaigns VALUES(?, ?, ?, ?)",
        [
            (booked_campaign, business_id, offering_id, "bookedSource12"),
            (other_campaign, business_id, offering_id, "otherSource123"),
        ],
    )
    conn.execute(
        "INSERT INTO promotion_events VALUES(?, ?, ?, ?, 'booked', ?)",
        (business_id, booked_campaign, "bookedSource12", customer_id, "2026-08-10T10:05:00+00:00"),
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


def test_two_sources_in_one_campaign_keep_separate_downstream_outcomes() -> None:
    conn = _db()
    business_id = str(uuid4())
    campaign_id = str(uuid4())
    offering_id = str(uuid4())
    source_a = "variantSourceA12"
    source_b = "variantSourceB12"
    customer_a = str(uuid4())
    customer_b = str(uuid4())
    lead_a = str(uuid4())
    conn.execute(
        "INSERT INTO promotion_campaigns VALUES(?, ?, ?, ?)",
        (campaign_id, business_id, offering_id, "campaignBase12"),
    )
    conn.executemany(
        "INSERT INTO promotion_events VALUES(?, ?, ?, ?, ?, ?)",
        [
            (business_id, campaign_id, source_a, customer_a, "opened", "2026-08-10T10:00:00+00:00"),
            (business_id, campaign_id, source_a, customer_a, "booked", "2026-08-10T10:05:00+00:00"),
            (business_id, campaign_id, source_b, customer_b, "opened", "2026-08-10T10:10:00+00:00"),
        ],
    )
    conn.execute(
        "INSERT INTO clientplatform_sales_leads VALUES(?, ?, ?, ?)",
        (lead_a, business_id, customer_a, offering_id),
    )
    conn.execute(
        "INSERT INTO clientplatform_sales_events VALUES(?, ?, 'conversation_transition', ?, ?)",
        (business_id, lead_a, json.dumps({"to": "won"}), "2026-08-10T10:20:00+00:00"),
    )

    result = load_promotion_attribution(
        conn,
        business_id=business_id,
        promotion_campaign_ids={campaign_id},
        event_from="2026-08-10T00:00:00+00:00",
        event_until="2026-08-11T00:00:00+00:00",
    )

    assert result.leads[campaign_id] == frozenset({customer_a, customer_b})
    assert result.source_leads[source_a] == frozenset({customer_a})
    assert result.source_leads[source_b] == frozenset({customer_b})
    assert result.source_bookings[source_a] == frozenset({customer_a})
    assert result.source_bookings.get(source_b, frozenset()) == frozenset()
    assert result.source_won[source_a] == frozenset({customer_a})


def test_legacy_null_source_falls_back_to_campaign_token() -> None:
    conn = _db()
    business_id = str(uuid4())
    campaign_id = str(uuid4())
    offering_id = str(uuid4())
    customer_id = str(uuid4())
    base_source = "legacySource123"
    conn.execute(
        "INSERT INTO promotion_campaigns VALUES(?, ?, ?, ?)",
        (campaign_id, business_id, offering_id, base_source),
    )
    conn.execute(
        "INSERT INTO promotion_events VALUES(?, ?, NULL, ?, 'opened', ?)",
        (business_id, campaign_id, customer_id, "2026-08-10T10:00:00+00:00"),
    )

    result = load_promotion_attribution(
        conn,
        business_id=business_id,
        promotion_campaign_ids={campaign_id},
        event_from="2026-08-10T00:00:00+00:00",
        event_until="2026-08-11T00:00:00+00:00",
    )

    assert result.source_leads[base_source] == frozenset({customer_id})
