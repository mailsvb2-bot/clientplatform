from __future__ import annotations

import json
import unittest

from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexDirectProvider,
    YandexOAuthConfig,
)


class RecordingTransport:
    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, *, method, url, headers, body=None, timeout=20.0):
        payload = json.loads(body.decode("utf-8")) if body else None
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "payload": payload,
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        return 200, {}, json.dumps(response).encode("utf-8")


def provider(transport: RecordingTransport) -> YandexDirectProvider:
    return YandexDirectProvider(
        oauth=YandexOAuthConfig(
            client_id="client-id",
            redirect_uri="https://oauth.yandex.ru/verification_code",
        ),
        transport=transport,
    )


def draft_campaign(*, state: str = "OFF", status: str = "DRAFT") -> dict:
    return {
        "Id": 7001,
        "Name": "ClientPlatform · cpmc_0123456789abcdef0123456789abcdef",
        "State": state,
        "Status": status,
        "Type": "UNIFIED_CAMPAIGN",
        "UnifiedCampaign": {
            "BiddingStrategy": {
                "Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                "Network": {"BiddingStrategyType": "SERVING_OFF"},
            }
        },
    }


class YandexManagedDraftStrategyTests(unittest.TestCase):
    def test_campaign_creation_uses_provider_valid_draft_strategy(self) -> None:
        transport = RecordingTransport({"result": {"AddResults": [{"Id": 7001}]}})
        direct = provider(transport)

        campaign_id = direct.create_disabled_managed_campaign(
            access_token="token",
            campaign_name="ClientPlatform · cpmc_0123456789abcdef0123456789abcdef",
            start_date="2026-08-17",
        )

        self.assertEqual(campaign_id, "7001")
        campaign = transport.calls[0]["payload"]["params"]["Campaigns"][0]
        strategy = campaign["UnifiedCampaign"]["BiddingStrategy"]
        self.assertEqual(
            strategy["Search"],
            {"BiddingStrategyType": "HIGHEST_POSITION"},
        )
        self.assertEqual(
            strategy["Network"],
            {"BiddingStrategyType": "SERVING_OFF"},
        )

    def test_managed_draft_publication_stays_draft_and_never_launches(self) -> None:
        transport = RecordingTransport(
            {"result": {"Campaigns": [draft_campaign()]}},
            {"result": {"AdGroups": []}},
            {"result": {"AddResults": [{"Id": 8001}]}},
            {"result": {"Ads": []}},
            {"result": {"AddResults": [{"Id": 9001}]}},
        )
        direct = provider(transport)

        result = direct.publish_managed_text_ad(
            access_token="token",
            external_campaign_id="7001",
            expected_campaign_name="ClientPlatform · cpmc_0123456789abcdef0123456789abcdef",
            region_ids=(213,),
            title="Консультация",
            text="Свободное время",
            href="https://example.test/offer",
            idempotency_key="job-1",
        )

        self.assertEqual(result.ad_group_id, "8001")
        self.assertEqual(result.ad_id, "9001")
        preflight = transport.calls[0]["payload"]["params"]
        self.assertIn("State", preflight["FieldNames"])
        self.assertIn("Status", preflight["FieldNames"])
        provider_methods = [call["payload"]["method"] for call in transport.calls]
        self.assertEqual(provider_methods, ["get", "get", "add", "get", "add"])
        self.assertNotIn("moderate", provider_methods)
        self.assertNotIn("resume", provider_methods)

    def test_managed_publication_fails_closed_if_campaign_is_not_off(self) -> None:
        transport = RecordingTransport(
            {"result": {"Campaigns": [draft_campaign(state="ON")]}}
        )
        direct = provider(transport)

        with self.assertRaisesRegex(
            YandexDirectError,
            "managed_campaign_state_not_off",
        ):
            direct.publish_managed_text_ad(
                access_token="token",
                external_campaign_id="7001",
                expected_campaign_name="ClientPlatform · cpmc_0123456789abcdef0123456789abcdef",
                region_ids=(213,),
                title="Консультация",
                text="Свободное время",
                href="https://example.test/offer",
                idempotency_key="job-1",
            )

        self.assertEqual(len(transport.calls), 1)

    def test_managed_publication_fails_closed_if_campaign_left_draft(self) -> None:
        transport = RecordingTransport(
            {"result": {"Campaigns": [draft_campaign(status="ACCEPTED")]}}
        )
        direct = provider(transport)

        with self.assertRaisesRegex(YandexDirectError, "managed_campaign_not_draft"):
            direct.publish_managed_text_ad(
                access_token="token",
                external_campaign_id="7001",
                expected_campaign_name="ClientPlatform · cpmc_0123456789abcdef0123456789abcdef",
                region_ids=(213,),
                title="Консультация",
                text="Свободное время",
                href="https://example.test/offer",
                idempotency_key="job-1",
            )

        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
