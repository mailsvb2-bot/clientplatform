from __future__ import annotations

import json
import unittest

from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexOAuthConfig,
)
from clientplatform.integrations.yandex_direct_moderation import (
    ModeratingYandexDirectProvider,
)


_GROUP_NAME = "ClientPlatform adjob_0123456789abcdef0123456789abcdef"
_DESTINATION = "https://t.me/clientplatform_bot?start=cpa_source"


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict[str, str], object]]):
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
        encoded = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )
        return status, response_headers, encoded


def _provider(transport: FakeTransport) -> ModeratingYandexDirectProvider:
    return ModeratingYandexDirectProvider(
        oauth=YandexOAuthConfig(
            client_id="client-id",
            redirect_uri=(
                "https://app.clientplatform.ru/"
                "oauth/yandex-direct/callback"
            ),
        ),
        transport=transport,
    )


def _publish(provider: ModeratingYandexDirectProvider):
    return provider.publish_text_ad(
        access_token="secret-token",
        external_campaign_id="6001",
        region_ids=(47,),
        title="Замена раковины",
        text="Свободное время у сантехника. Запишитесь онлайн.",
        href=_DESTINATION,
        idempotency_key="adjob_0123456789abcdef0123456789abcdef",
    )


def _campaign_response(
    campaign_type: str = "TEXT_CAMPAIGN",
    *,
    state: str = "ON",
    status: str = "ACCEPTED",
):
    return (
        200,
        {},
        {
            "result": {
                "Campaigns": [
                    {
                        "Id": 6001,
                        "State": state,
                        "Status": status,
                        "Type": campaign_type,
                    }
                ]
            }
        },
    )


def _methods(transport: FakeTransport) -> list[str]:
    return [json.loads(call["body"])["method"] for call in transport.calls]


def _services(transport: FakeTransport) -> list[str]:
    return [str(call["url"]).rsplit("/", 1)[-1] for call in transport.calls]


class YandexDraftSafetyTests(unittest.TestCase):
    def test_catalog_exposes_only_active_accepted_paid_ready_text_campaigns(self) -> None:
        transport = FakeTransport(
            [
                (
                    200,
                    {},
                    {
                        "result": {
                            "Campaigns": [
                                {
                                    "Id": 6001,
                                    "Name": "Текстовая кампания",
                                    "State": "ON",
                                    "Status": "ACCEPTED",
                                    "StatusPayment": "ALLOWED",
                                    "Type": "TEXT_CAMPAIGN",
                                },
                                {
                                    "Id": 6002,
                                    "Name": "Без оплаты",
                                    "State": "ON",
                                    "Status": "ACCEPTED",
                                    "StatusPayment": "DISALLOWED",
                                    "Type": "TEXT_CAMPAIGN",
                                },
                                {
                                    "Id": 6003,
                                    "Name": "Единая кампания",
                                    "State": "ON",
                                    "Status": "ACCEPTED",
                                    "StatusPayment": "ALLOWED",
                                    "Type": "UNIFIED_CAMPAIGN",
                                },
                            ]
                        }
                    },
                )
            ]
        )

        campaigns = _provider(transport).list_text_campaigns(
            access_token="secret-token"
        )

        self.assertEqual(
            [(item.campaign_id, item.campaign_type) for item in campaigns],
            [("6001", "TEXT_CAMPAIGN")],
        )
        request = json.loads(transport.calls[0]["body"])
        criteria = request["params"]["SelectionCriteria"]
        self.assertEqual(criteria["Types"], ["TEXT_CAMPAIGN"])
        self.assertEqual(criteria["States"], ["ON"])
        self.assertEqual(criteria["Statuses"], ["ACCEPTED"])
        self.assertEqual(criteria["StatusesPayment"], ["ALLOWED"])
        self.assertIn("StatusPayment", request["params"]["FieldNames"])

    def test_publication_creates_draft_without_moderation_or_keywords(self) -> None:
        transport = FakeTransport(
            [
                _campaign_response(),
                (200, {}, {"result": {"AdGroups": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 7001}]}}),
                (200, {}, {"result": {"Ads": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 8001}]}}),
                (200, {}, {"result": {"Ads": [{"Id": 8001, "Status": "DRAFT"}]}}),
            ]
        )

        result = _publish(_provider(transport))

        self.assertEqual((result.ad_group_id, result.ad_id), ("7001", "8001"))
        self.assertNotIn("moderate", _methods(transport))
        self.assertNotIn("keywords", _services(transport))
        for call in transport.calls:
            self.assertEqual(
                call["headers"]["Authorization"],
                "Bearer secret-token",
            )
            self.assertNotIn("secret-token", str(call["body"]))

    def test_existing_draft_objects_are_reconciled_without_mutation(self) -> None:
        transport = FakeTransport(
            [
                _campaign_response(),
                (
                    200,
                    {},
                    {"result": {"AdGroups": [{"Id": 7001, "Name": _GROUP_NAME}]}},
                ),
                (
                    200,
                    {},
                    {
                        "result": {
                            "Ads": [
                                {"Id": 8001, "TextAd": {"Href": _DESTINATION}}
                            ]
                        }
                    },
                ),
                (200, {}, {"result": {"Ads": [{"Id": 8001, "Status": "DRAFT"}]}}),
            ]
        )

        result = _publish(_provider(transport))

        self.assertEqual((result.ad_group_id, result.ad_id), ("7001", "8001"))
        self.assertEqual(_methods(transport), ["get"] * 4)
        self.assertNotIn("keywords", _services(transport))

    def test_non_draft_existing_ad_fails_closed_without_targeting_changes(self) -> None:
        for status in ("MODERATION", "PREACCEPTED", "ACCEPTED", "REJECTED"):
            with self.subTest(status=status):
                transport = FakeTransport(
                    [
                        _campaign_response(),
                        (
                            200,
                            {},
                            {
                                "result": {
                                    "AdGroups": [
                                        {"Id": 7001, "Name": _GROUP_NAME}
                                    ]
                                }
                            },
                        ),
                        (
                            200,
                            {},
                            {
                                "result": {
                                    "Ads": [
                                        {
                                            "Id": 8001,
                                            "TextAd": {"Href": _DESTINATION},
                                        }
                                    ]
                                }
                            },
                        ),
                        (
                            200,
                            {},
                            {"result": {"Ads": [{"Id": 8001, "Status": status}]}},
                        ),
                    ]
                )
                with self.assertRaises(YandexDirectError) as raised:
                    _publish(_provider(transport))
                self.assertEqual(
                    raised.exception.code,
                    "existing_ad_is_not_draft",
                )
                self.assertNotIn("moderate", _methods(transport))
                self.assertNotIn("keywords", _services(transport))

    def test_unified_or_ineligible_campaign_fails_before_creation(self) -> None:
        cases = (
            _campaign_response("UNIFIED_CAMPAIGN"),
            _campaign_response(state="OFF"),
            _campaign_response(status="DRAFT"),
        )
        for response in cases:
            with self.subTest(response=response):
                transport = FakeTransport([response])
                with self.assertRaises(YandexDirectError):
                    _publish(_provider(transport))
                self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()