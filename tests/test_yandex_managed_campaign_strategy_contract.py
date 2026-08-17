from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexDirectProvider,
    YandexOAuthConfig,
)


_CAMPAIGN_ID = 123456
_CAMPAIGN_NAME = "ClientPlatform · cpmc_" + ("a" * 32)


class _CampaignTransport:
    def __init__(
        self,
        *,
        state: str = "OFF",
        status: str = "DRAFT",
        search_strategy: str = "HIGHEST_POSITION",
        network_strategy: str = "SERVING_OFF",
    ) -> None:
        self.state = state
        self.status = status
        self.search_strategy = search_strategy
        self.network_strategy = network_strategy
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout: float = 20.0,
    ) -> tuple[int, Mapping[str, str], bytes]:
        del headers, timeout
        assert method == "POST"
        assert body is not None
        payload = json.loads(body.decode("utf-8"))
        self.calls.append({"url": url, "payload": payload})
        if payload["method"] == "add":
            response = {"result": {"AddResults": [{"Id": _CAMPAIGN_ID}]}}
        elif payload["method"] == "get":
            response = {
                "result": {
                    "Campaigns": [
                        {
                            "Id": _CAMPAIGN_ID,
                            "Name": _CAMPAIGN_NAME,
                            "State": self.state,
                            "Status": self.status,
                            "Type": "UNIFIED_CAMPAIGN",
                            "UnifiedCampaign": {
                                "BiddingStrategy": {
                                    "Search": {
                                        "BiddingStrategyType": self.search_strategy,
                                    },
                                    "Network": {
                                        "BiddingStrategyType": self.network_strategy,
                                    },
                                }
                            },
                        }
                    ]
                }
            }
        else:  # pragma: no cover - regression guard
            raise AssertionError(f"unexpected Yandex method: {payload['method']}")
        return 200, {}, json.dumps(response).encode("utf-8")


def _provider(transport: _CampaignTransport) -> YandexDirectProvider:
    return YandexDirectProvider(
        oauth=YandexOAuthConfig(
            client_id="client-id",
            redirect_uri="https://app.clientplatform.ru/oauth/yandex/callback",
        ),
        transport=transport,
    )


def test_managed_campaign_add_uses_provider_valid_non_network_strategy_pair() -> None:
    transport = _CampaignTransport()
    provider = _provider(transport)

    campaign_id = provider.create_disabled_managed_campaign(
        access_token="opaque-token",
        campaign_name=_CAMPAIGN_NAME,
        start_date="2026-08-17",
    )

    assert campaign_id == str(_CAMPAIGN_ID)
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"].endswith("/json/v501/campaigns")
    payload = call["payload"]
    assert payload["method"] == "add"
    campaign = payload["params"]["Campaigns"][0]
    assert campaign["UnifiedCampaign"]["BiddingStrategy"] == {
        "Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
        "Network": {"BiddingStrategyType": "SERVING_OFF"},
    }
    assert "DailyBudget" not in campaign
    assert {item["payload"]["method"] for item in transport.calls} == {"add"}


def test_managed_campaign_guard_requires_off_draft_and_exact_strategy_pair() -> None:
    transport = _CampaignTransport()
    provider = _provider(transport)

    provider._assert_managed_campaign_non_serving(
        access_token="opaque-token",
        campaign_id=_CAMPAIGN_ID,
        expected_campaign_name=_CAMPAIGN_NAME,
    )

    params = transport.calls[0]["payload"]["params"]
    assert params["FieldNames"] == ["Id", "Name", "State", "Status", "Type"]
    assert params["UnifiedCampaignFieldNames"] == ["BiddingStrategy"]


@pytest.mark.parametrize(
    ("transport", "expected_code"),
    [
        (_CampaignTransport(state="ON"), "managed_campaign_serving_is_enabled"),
        (_CampaignTransport(status="MODERATION"), "managed_campaign_not_draft"),
        (
            _CampaignTransport(search_strategy="SERVING_OFF"),
            "managed_campaign_strategy_mismatch",
        ),
        (
            _CampaignTransport(network_strategy="NETWORK_DEFAULT"),
            "managed_campaign_strategy_mismatch",
        ),
    ],
)
def test_managed_campaign_guard_fails_closed(
    transport: _CampaignTransport,
    expected_code: str,
) -> None:
    provider = _provider(transport)

    with pytest.raises(YandexDirectError) as exc_info:
        provider._assert_managed_campaign_non_serving(
            access_token="opaque-token",
            campaign_id=_CAMPAIGN_ID,
            expected_campaign_name=_CAMPAIGN_NAME,
        )

    assert exc_info.value.code == expected_code
