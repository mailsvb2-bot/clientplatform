from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application.event_analytics import CurrencyRevenue
from clientplatform.application.event_growth import (
    EventAcquisitionSlice,
    build_event_registration_url,
    build_yandex_event_registration_url,
    event_roas,
    get_event_acquisition_breakdown_in_transaction,
)
from clientplatform.domain.events import EventValidationError


def test_event_registration_url_preserves_campaign_dimensions() -> None:
    url = build_event_registration_url(
        public_base_url="https://clientplatform.example/",
        public_slug="AbCdEf0123456789_-AbCdEf0123456789",
        source="yandex_direct",
        campaign_ref="123456789",
    )
    assert url == (
        "https://clientplatform.example/e/AbCdEf0123456789_-AbCdEf0123456789"
        "?source=yandex_direct&campaign_ref=123456789"
    )


def test_event_registration_url_encodes_dimensions() -> None:
    url = build_event_registration_url(
        public_base_url="https://clientplatform.example",
        public_slug="AbCdEf0123456789_-AbCdEf0123456789",
        source="partner / webinar",
        campaign_ref="launch 1+2",
    )
    assert "source=partner+%2F+webinar" in url
    assert "campaign_ref=launch+1%2B2" in url


def test_event_registration_url_rejects_non_https_base() -> None:
    with unittest.TestCase().assertRaises(EventValidationError):
        build_event_registration_url(
            public_base_url="http://clientplatform.example",
            public_slug="AbCdEf0123456789_-AbCdEf0123456789",
        )


def test_event_roas_combines_only_matching_currency() -> None:
    acquisition = EventAcquisitionSlice(
        source="yandex_direct",
        campaign_ref="42",
        registered=100,
        join_clicked=60,
        attendance_confirmed=50,
        offer_clicked=20,
        paid=5,
        revenue=(
            CurrencyRevenue(currency="RUB", amount_minor=250_000),
            CurrencyRevenue(currency="USD", amount_minor=1_000),
        ),
    )
    result = event_roas(acquisition, spend_minor=100_000, currency="rub")
    assert result.revenue_minor == 250_000
    assert result.roas == 2.5
    assert result.profit_after_ad_spend_minor == 150_000
    assert acquisition.paid_rate == 0.05


def test_yandex_event_url_uses_provider_campaign_id_as_attribution_reference() -> None:
    url = build_yandex_event_registration_url(
        public_base_url="https://clientplatform.example",
        public_slug="AbCdEf0123456789_-AbCdEf0123456789",
        external_campaign_id="987654321",
    )
    assert url.endswith("?source=yandex_direct&campaign_ref=987654321")


def test_event_registration_url_rejects_unicode_slug_even_if_isalnum() -> None:
    import unittest
    from clientplatform.domain.events import EventValidationError

    with unittest.TestCase().assertRaises(EventValidationError):
        build_event_registration_url(
            public_base_url="https://clientplatform.example",
            public_slug="я" * 24,
        )



class _Rows:
    def fetchall(self):
        return []


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...]):
        self.calls.append((" ".join(sql.split()), tuple(params)))
        return _Rows()


def test_event_acquisition_grouping_reuses_selected_dimensions_on_postgres() -> None:
    conn = _RecordingConnection()
    actor = object()
    event = SimpleNamespace(business_id="business-1", id="event-1")

    with patch(
        "clientplatform.application.event_growth.EventRepository"
    ) as repository:
        repository.return_value.get.return_value = event
        result = get_event_acquisition_breakdown_in_transaction(
            conn,
            actor=actor,  # type: ignore[arg-type]
            event_id=event.id,
        )

    assert result == ()
    assert len(conn.calls) == 2

    metric_sql, metric_params = conn.calls[0]
    revenue_sql, revenue_params = conn.calls[1]
    assert "GROUP BY 1, 2" in metric_sql
    assert "GROUP BY 1, 2, 3" in revenue_sql
    assert metric_params == ("direct", event.business_id, event.id)
    assert revenue_params == ("direct", event.business_id, event.id)
