from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import yandex_growth_analytics as growth
from clientplatform.integrations.yandex_direct_analytics import YandexAdPerformanceRow


def _tracked(
    *,
    connection_id: str,
    promotion_id: str,
    campaign_id: str,
    ad_id: str,
) -> growth._TrackedAd:
    return growth._TrackedAd(
        connection_id=connection_id,
        external_login=f"login-{connection_id}",
        promotion_campaign_id=promotion_id,
        external_campaign_id=campaign_id,
        external_campaign_name=f"Campaign {campaign_id}",
        external_ad_id=ad_id,
    )


def test_report_batches_never_cross_provider_ad_limit() -> None:
    batches = growth._ad_id_batches(tuple(str(index) for index in range(1, 502)))
    assert [len(batch) for batch in batches] == [500, 1]
    assert batches[0][0] == "1"
    assert batches[-1] == ("501",)


def test_multi_connection_snapshot_never_sums_unknown_currencies() -> None:
    actor = SimpleNamespace(user_id=101, business_id="business")
    tracked = [
        _tracked(
            connection_id="connection-a",
            promotion_id="promotion-a",
            campaign_id="6001",
            ad_id="9001",
        ),
        _tracked(
            connection_id="connection-b",
            promotion_id="promotion-b",
            campaign_id="6002",
            ad_id="9002",
        ),
    ]
    rows = {
        ("connection-a", "9001"): YandexAdPerformanceRow(
            ad_id="9001",
            campaign_id="6001",
            campaign_name="A",
            impressions=100,
            clicks=10,
            cost_micros=10_000_000,
        ),
        ("connection-b", "9002"): YandexAdPerformanceRow(
            ad_id="9002",
            campaign_id="6002",
            campaign_name="B",
            impressions=200,
            clicks=20,
            cost_micros=20_000_000,
        ),
    }
    empty = growth._LocalAttribution(leads={}, bookings={}, won={})

    with (
        patch.object(growth, "_load_tracked_ads", return_value=(actor, 2, tracked)),
        patch.object(growth, "_provider_rows", return_value=rows),
        patch.object(growth, "_load_local_attribution", return_value=empty),
    ):
        snapshot = growth.get_yandex_growth_snapshot(
            actor=actor,
            period_days=7,
            provider=object(),
            vault=object(),
        )

    assert snapshot.impressions == 300
    assert snapshot.clicks == 30
    assert snapshot.cost_micros is None
    assert snapshot.cpc_micros is None
    assert snapshot.cpl_micros is None
    assert snapshot.booking_cost_micros is None
    assert snapshot.cac_micros is None
    assert [campaign.cost_micros for campaign in snapshot.campaigns] == [20_000_000, 10_000_000]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _AttributionConnection:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, sql: str, _params=()):
        normalized = " ".join(sql.split())
        self.sql.append(normalized)
        if "FROM promotion_events" in normalized and "JOIN promotion_campaigns" not in normalized:
            return _Result(
                [
                    {"campaign_id": "promotion-a", "event_type": "opened", "customer_id": "customer"},
                    {"campaign_id": "promotion-a", "event_type": "booked", "customer_id": "customer"},
                ]
            )
        if "JOIN promotion_campaigns" in normalized:
            return _Result(
                [
                    {
                        "campaign_id": "promotion-a",
                        "customer_id": "customer",
                        "payload_json": '{"to":"qualified"}',
                    },
                    {
                        "campaign_id": "promotion-a",
                        "customer_id": "customer",
                        "payload_json": '{"to":"won"}',
                    },
                ]
            )
        raise AssertionError(normalized)


class _Tenancy:
    def __init__(self, _conn) -> None:
        pass

    def resolve_context(self, *, user_id, business_id):
        return SimpleNamespace(
            user_id=user_id,
            business_id=business_id,
            assert_can_view_promotion_analytics=lambda: None,
        )


def test_won_attribution_requires_booking_offering_and_post_booking_transition() -> None:
    conn = _AttributionConnection()

    @contextmanager
    def read_db():
        yield conn

    actor = SimpleNamespace(user_id=101, business_id="business")
    with (
        patch.object(growth, "get_db_ro", read_db),
        patch.object(growth, "TenancyRepository", _Tenancy),
    ):
        attribution = growth._load_local_attribution(
            actor=actor,
            promotion_campaign_ids={"promotion-a"},
            date_from="2026-08-03",
            date_to="2026-08-09",
        )

    assert attribution.leads["promotion-a"] == frozenset({"customer"})
    assert attribution.bookings["promotion-a"] == frozenset({"customer"})
    assert attribution.won["promotion-a"] == frozenset({"customer"})
    won_sql = next(sql for sql in conn.sql if "JOIN promotion_campaigns" in sql)
    assert "pe.event_type='booked'" in won_sql
    assert "sl.offering_id=pc.offering_id" in won_sql
    assert "se.event_type='conversation_transition'" in won_sql
    assert "se.occurred_at>=pe.occurred_at" in won_sql
