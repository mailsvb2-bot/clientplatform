from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.application import yandex_campaign_diagnostics as diagnostics
from clientplatform.integrations.yandex_direct import YandexDirectError, YandexTokenBundle
from clientplatform.integrations.yandex_direct_analytics import (
    YandexCampaignPerformanceReport,
    YandexCampaignPerformanceRow,
)


class _Context:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


class _Store:
    def __init__(self, token_json: str) -> None:
        self.token_json = token_json

    def load_active(self, *, business_id: str, connection_id: str):
        return SimpleNamespace(id=connection_id, business_id=business_id), self.token_json


class _Provider:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    def campaign_performance_report(self, **kwargs):
        token = str(kwargs["access_token"])
        self.tokens.append(token)
        if len(self.tokens) == 1:
            raise YandexDirectError("provider_http_401")
        return YandexCampaignPerformanceReport(
            date_from=str(kwargs["date_from"]),
            date_to=str(kwargs["date_to"]),
            rows=(
                YandexCampaignPerformanceRow(
                    campaign_id="6001",
                    campaign_name="Campaign",
                    impressions=10,
                    clicks=2,
                    cost_micros=3_000_000,
                ),
            ),
        )


def test_campaign_report_retries_once_after_canonical_token_refresh() -> None:
    business_id = str(uuid4())
    connection_id = str(uuid4())
    old_bundle = YandexTokenBundle(
        access_token="expired-token",
        token_type="bearer",
        expires_in=3600,
        refresh_token="refresh-token",
        scope=("direct:api",),
    )
    new_bundle = YandexTokenBundle(
        access_token="fresh-token",
        token_type="bearer",
        expires_in=3600,
        refresh_token="refresh-token-2",
        scope=("direct:api",),
    )
    tracked = [
        diagnostics._TrackedCampaign(
            connection_id=connection_id,
            external_login="owner-login",
            campaign_id="6001",
            campaign_name="Campaign",
        )
    ]
    provider = _Provider()
    store = _Store(old_bundle.to_json())

    with (
        patch.object(diagnostics, "get_db_ro", return_value=_Context()),
        patch.object(diagnostics, "AdWorkerStore", return_value=store),
        patch.object(diagnostics, "_refresh_bundle", return_value=new_bundle) as refresh,
    ):
        rows = diagnostics._provider_rows(
            current=SimpleNamespace(business_id=business_id),
            tracked=tracked,
            date_from="2026-08-03",
            date_to="2026-08-09",
            vault=object(),
            provider=provider,
        )

    assert provider.tokens == ["expired-token", "fresh-token"]
    refresh.assert_called_once()
    assert rows[(connection_id, "6001")].clicks == 2
