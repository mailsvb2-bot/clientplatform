from __future__ import annotations

import json

import pytest

from clientplatform.domain.managed_ad_campaigns import (
    managed_campaign_name,
    managed_campaign_provisioning_key,
)
from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexDirectProvider,
    YandexOAuthConfig,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, *, method, url, headers, body=None, timeout=20.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        status, response_headers, payload = self.responses.pop(0)
        return status, response_headers, json.dumps(payload).encode("utf-8")


def _provider(transport: FakeTransport) -> YandexDirectProvider:
    return YandexDirectProvider(
        oauth=YandexOAuthConfig(
            client_id="client-id",
            redirect_uri="https://app.clientplatform.ru/oauth/yandex-direct/callback",
        ),
        transport=transport,
    )


def _managed_name() -> str:
    key = managed_campaign_provisioning_key(
        business_id="00000000-0000-4000-8000-000000000001",
        promotion_campaign_id="00000000-0000-4000-8000-000000000002",
        connection_id="00000000-0000-4000-8000-000000000003",
    )
    return managed_campaign_name(key)


def test_managed_campaign_key_is_deterministic_and_opaque() -> None:
    value = managed_campaign_provisioning_key(
        business_id="00000000-0000-4000-8000-000000000001",
        promotion_campaign_id="00000000-0000-4000-8000-000000000002",
        connection_id="00000000-0000-4000-8000-000000000003",
    )
    assert value == managed_campaign_provisioning_key(
        business_id="00000000-0000-4000-8000-000000000001",
        promotion_campaign_id="00000000-0000-4000-8000-000000000002",
        connection_id="00000000-0000-4000-8000-000000000003",
    )
    assert value.startswith("cpmc_")
    assert "00000000-0000" not in value


def test_managed_campaign_create_uses_v501_and_serving_off_without_budget() -> None:
    transport = FakeTransport(
        [(200, {}, {"result": {"AddResults": [{"Id": 61001}]}})]
    )
    provider = _provider(transport)
    name = _managed_name()

    campaign_id = provider.create_disabled_managed_campaign(
        access_token="secret-token",
        campaign_name=name,
    )

    assert campaign_id == "61001"
    assert transport.calls[0]["url"].endswith("/json/v501/campaigns")
    payload = json.loads(transport.calls[0]["body"])
    campaign = payload["params"]["Campaigns"][0]
    assert campaign["Name"] == name
    strategies = campaign["UnifiedCampaign"]["BiddingStrategy"]
    assert strategies["Search"]["BiddingStrategyType"] == "SERVING_OFF"
    assert strategies["Network"]["BiddingStrategyType"] == "SERVING_OFF"
    assert "budget" not in json.dumps(payload).lower()
    assert "secret-token" not in str(transport.calls[0]["body"])


def test_managed_campaign_lookup_requires_exact_opaque_marker() -> None:
    name = _managed_name()
    transport = FakeTransport(
        [
            (
                200,
                {},
                {
                    "result": {
                        "Campaigns": [
                            {
                                "Id": 61000,
                                "Name": "Not ours",
                                "State": "OFF",
                                "Status": "DRAFT",
                                "Type": "UNIFIED_CAMPAIGN",
                            },
                            {
                                "Id": 61001,
                                "Name": name,
                                "State": "OFF",
                                "Status": "DRAFT",
                                "Type": "UNIFIED_CAMPAIGN",
                            },
                        ]
                    }
                },
            )
        ]
    )
    found = _provider(transport).find_managed_campaign(
        access_token="secret-token",
        campaign_name=name,
    )
    assert found is not None
    assert found.campaign_id == "61001"
    assert found.name == name


def test_managed_publication_rechecks_non_serving_and_uses_unified_group() -> None:
    name = _managed_name()
    transport = FakeTransport(
        [
            (
                200,
                {},
                {
                    "result": {
                        "Campaigns": [
                            {
                                "Id": 61001,
                                "Name": name,
                                "Type": "UNIFIED_CAMPAIGN",
                                "UnifiedCampaign": {
                                    "BiddingStrategy": {
                                        "Search": {"BiddingStrategyType": "SERVING_OFF"},
                                        "Network": {"BiddingStrategyType": "SERVING_OFF"},
                                    }
                                },
                            }
                        ]
                    }
                },
            ),
            (200, {}, {"result": {"AdGroups": []}}),
            (200, {}, {"result": {"AddResults": [{"Id": 71001}]}}),
            (200, {}, {"result": {"Ads": []}}),
            (200, {}, {"result": {"AddResults": [{"Id": 81001}]}}),
        ]
    )
    result = _provider(transport).publish_managed_text_ad(
        access_token="secret-token",
        external_campaign_id="61001",
        expected_campaign_name=name,
        region_ids=(47,),
        title="Замена раковины",
        text="Свободное время у сантехника. Запишитесь онлайн.",
        href="https://t.me/clientplatform_bot?start=cpa_source",
        idempotency_key="adjob_0123456789abcdef0123456789abcdef",
    )
    assert result.ad_group_id == "71001"
    assert result.ad_id == "81001"
    assert all("/json/v501/" in str(call["url"]) for call in transport.calls)
    group_payload = json.loads(transport.calls[2]["body"])
    group = group_payload["params"]["AdGroups"][0]
    assert group["UnifiedAdGroup"] == {"OfferRetargeting": "NO"}
    ad_payload = json.loads(transport.calls[4]["body"])
    text_ad = ad_payload["params"]["Ads"][0]["TextAd"]
    assert "Mobile" not in text_ad


def test_managed_publication_fails_closed_if_serving_was_enabled() -> None:
    name = _managed_name()
    transport = FakeTransport(
        [
            (
                200,
                {},
                {
                    "result": {
                        "Campaigns": [
                            {
                                "Id": 61001,
                                "Name": name,
                                "Type": "UNIFIED_CAMPAIGN",
                                "UnifiedCampaign": {
                                    "BiddingStrategy": {
                                        "Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                                        "Network": {"BiddingStrategyType": "SERVING_OFF"},
                                    }
                                },
                            }
                        ]
                    }
                },
            )
        ]
    )
    with pytest.raises(YandexDirectError) as error:
        _provider(transport).publish_managed_text_ad(
            access_token="secret-token",
            external_campaign_id="61001",
            expected_campaign_name=name,
            region_ids=(47,),
            title="Замена раковины",
            text="Свободное время у сантехника. Запишитесь онлайн.",
            href="https://t.me/clientplatform_bot?start=cpa_source",
            idempotency_key="adjob_0123456789abcdef0123456789abcdef",
        )
    assert error.value.code == "managed_campaign_serving_is_enabled"
    assert len(transport.calls) == 1
