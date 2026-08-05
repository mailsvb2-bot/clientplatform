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


def _campaign_response(campaign_type: str = "TEXT_CAMPAIGN"):
    return (
        200,
        {},
        {
            "result": {
                "Campaigns": [
                    {
                        "Id": 6001,
                        "State": "ON",
                        "Status": "ACCEPTED",
                        "Type": campaign_type,
                    }
                ]
            }
        },
    )


def _existing_group_response():
    return (
        200,
        {},
        {"result": {"AdGroups": [{"Id": 7001, "Name": _GROUP_NAME}]}},
    )


def _existing_legacy_ad_response():
    return (
        200,
        {},
        {
            "result": {
                "Ads": [
                    {"Id": 8001, "TextAd": {"Href": _DESTINATION}}
                ]
            }
        },
    )


def _existing_responsive_ad_response():
    return (
        200,
        {},
        {
            "result": {
                "Ads": [
                    {"Id": 8001, "ResponsiveAd": {"Href": _DESTINATION}}
                ]
            }
        },
    )


def _ad_status_response(status: str):
    return (
        200,
        {},
        {"result": {"Ads": [{"Id": 8001, "Status": status}]}},
    )


def _keyword_response(*, state: str = "ON"):
    return (
        200,
        {},
        {
            "result": {
                "Keywords": [
                    {
                        "Id": 9001,
                        "Keyword": "Замена раковины",
                        "State": state,
                        "Status": "ACCEPTED",
                    }
                ]
            }
        },
    )


def _methods(transport: FakeTransport) -> list[str]:
    return [json.loads(call["body"])["method"] for call in transport.calls]


class YandexModerationTests(unittest.TestCase):
    def test_campaign_catalog_includes_only_active_accepted_supported(self) -> None:
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
                                    "Name": "Старая кампания",
                                    "State": "ON",
                                    "Status": "ACCEPTED",
                                    "Type": "TEXT_CAMPAIGN",
                                },
                                {
                                    "Id": 6002,
                                    "Name": "Единая кампания",
                                    "State": "ON",
                                    "Status": "ACCEPTED",
                                    "Type": "UNIFIED_CAMPAIGN",
                                },
                                {
                                    "Id": 6003,
                                    "Name": "Выключенная кампания",
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
        campaigns = _provider(transport).list_text_campaigns(
            access_token="secret-token"
        )
        self.assertEqual(
            [(item.campaign_id, item.campaign_type) for item in campaigns],
            [
                ("6001", "TEXT_CAMPAIGN"),
                ("6002", "UNIFIED_CAMPAIGN"),
            ],
        )
        request = json.loads(transport.calls[0]["body"])
        selection = request["params"]["SelectionCriteria"]
        self.assertEqual(
            set(selection["Types"]),
            {"TEXT_CAMPAIGN", "UNIFIED_CAMPAIGN"},
        )
        self.assertEqual(selection["States"], ["ON"])
        self.assertEqual(selection["Statuses"], ["ACCEPTED"])
        self.assertIn("/json/v501/campaigns", transport.calls[0]["url"])

    def test_new_legacy_text_ad_is_submitted_for_moderation(self) -> None:
        transport = FakeTransport(
            [
                _campaign_response(),
                (200, {}, {"result": {"AdGroups": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 7001}]}}),
                (200, {}, {"result": {"Ads": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 8001}]}}),
                _ad_status_response("DRAFT"),
                (
                    200,
                    {},
                    {"result": {"ModerateResults": [{"Id": 8001}]}},
                ),
            ]
        )
        result = _publish(_provider(transport))
        self.assertEqual(result.ad_group_id, "7001")
        self.assertEqual(result.ad_id, "8001")
        self.assertEqual(len(transport.calls), 7)
        add_ad = json.loads(transport.calls[4]["body"])
        self.assertIn("TextAd", add_ad["params"]["Ads"][0])
        moderate = json.loads(transport.calls[-1]["body"])
        self.assertEqual(moderate["method"], "moderate")
        self.assertEqual(
            moderate["params"]["SelectionCriteria"]["Ids"],
            [8001],
        )
        for call in transport.calls:
            self.assertEqual(
                call["headers"]["Authorization"],
                "Bearer secret-token",
            )
            self.assertNotIn("secret-token", str(call["body"]))
            self.assertIn("/json/v501/", call["url"])

    def test_new_unified_campaign_creates_targeting_and_moderates(self) -> None:
        transport = FakeTransport(
            [
                _campaign_response("UNIFIED_CAMPAIGN"),
                (200, {}, {"result": {"AdGroups": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 7001}]}}),
                (200, {}, {"result": {"Ads": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 8001}]}}),
                (200, {}, {"result": {"Keywords": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 9001}]}}),
                _ad_status_response("DRAFT"),
                (
                    200,
                    {},
                    {"result": {"ModerateResults": [{"Id": 8001}]}},
                ),
            ]
        )
        result = _publish(_provider(transport))
        self.assertEqual((result.ad_group_id, result.ad_id), ("7001", "8001"))

        add_group = json.loads(transport.calls[2]["body"])
        self.assertEqual(
            add_group["params"]["AdGroups"][0]["UnifiedAdGroup"],
            {"OfferRetargeting": "NO"},
        )
        add_ad = json.loads(transport.calls[4]["body"])
        responsive = add_ad["params"]["Ads"][0]["ResponsiveAd"]
        self.assertEqual(responsive["Titles"], ["Замена раковины"])
        self.assertEqual(responsive["Href"], _DESTINATION)
        self.assertNotIn("TextAd", add_ad["params"]["Ads"][0])

        add_keyword = json.loads(transport.calls[6]["body"])
        self.assertEqual(
            add_keyword["params"]["Keywords"][0],
            {"AdGroupId": 7001, "Keyword": "Замена раковины"},
        )
        self.assertEqual(
            _methods(transport),
            ["get", "get", "add", "get", "add", "get", "add", "get", "moderate"],
        )

    def test_existing_unified_objects_are_reused_without_duplicate_actions(self) -> None:
        transport = FakeTransport(
            [
                _campaign_response("UNIFIED_CAMPAIGN"),
                _existing_group_response(),
                _existing_responsive_ad_response(),
                _keyword_response(),
                _ad_status_response("ACCEPTED"),
            ]
        )
        result = _publish(_provider(transport))
        self.assertEqual((result.ad_group_id, result.ad_id), ("7001", "8001"))
        self.assertEqual(_methods(transport), ["get"] * 5)

    def test_suspended_unified_keyword_is_resumed_not_duplicated(self) -> None:
        transport = FakeTransport(
            [
                _campaign_response("UNIFIED_CAMPAIGN"),
                _existing_group_response(),
                _existing_responsive_ad_response(),
                _keyword_response(state="SUSPENDED"),
                (200, {}, {"result": {"ResumeResults": [{"Id": 9001}]}}),
                _ad_status_response("ACCEPTED"),
            ]
        )
        _publish(_provider(transport))
        self.assertEqual(
            _methods(transport),
            ["get", "get", "get", "get", "resume", "get"],
        )
        resume = json.loads(transport.calls[4]["body"])
        self.assertEqual(
            resume["params"]["SelectionCriteria"]["Ids"],
            [9001],
        )

    def test_existing_legacy_draft_is_moderated_without_duplicates(self) -> None:
        transport = FakeTransport(
            [
                _campaign_response(),
                _existing_group_response(),
                _existing_legacy_ad_response(),
                _ad_status_response("DRAFT"),
                (
                    200,
                    {},
                    {"result": {"ModerateResults": [{"Id": 8001}]}},
                ),
            ]
        )
        result = _publish(_provider(transport))
        self.assertEqual((result.ad_group_id, result.ad_id), ("7001", "8001"))
        self.assertEqual(
            _methods(transport),
            ["get", "get", "get", "get", "moderate"],
        )

    def test_reviewing_or_accepted_legacy_ad_is_not_moderated_again(self) -> None:
        for status in ("MODERATION", "PREACCEPTED", "ACCEPTED"):
            with self.subTest(status=status):
                transport = FakeTransport(
                    [
                        _campaign_response(),
                        _existing_group_response(),
                        _existing_legacy_ad_response(),
                        _ad_status_response(status),
                    ]
                )
                _publish(_provider(transport))
                self.assertEqual(_methods(transport), ["get"] * 4)

    def test_rejected_ad_requires_manual_review_instead_of_resubmission(self) -> None:
        transport = FakeTransport(
            [
                _campaign_response(),
                _existing_group_response(),
                _existing_legacy_ad_response(),
                _ad_status_response("REJECTED"),
            ]
        )
        with self.assertRaises(YandexDirectError) as raised:
            _publish(_provider(transport))
        self.assertEqual(
            raised.exception.code,
            "ad_rejected_requires_manual_review",
        )
        self.assertEqual(len(transport.calls), 4)

    def test_moderation_error_is_reduced_to_safe_provider_code(self) -> None:
        transport = FakeTransport(
            [
                _campaign_response(),
                (200, {}, {"result": {"AdGroups": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 7001}]}}),
                (200, {}, {"result": {"Ads": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 8001}]}}),
                _ad_status_response("DRAFT"),
                (
                    200,
                    {},
                    {
                        "result": {
                            "ModerateResults": [
                                {
                                    "Errors": [
                                        {
                                            "Code": 8800,
                                            "Message": (
                                                "sensitive provider detail"
                                            ),
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                ),
            ]
        )
        with self.assertRaises(YandexDirectError) as raised:
            _publish(_provider(transport))
        self.assertEqual(raised.exception.code, "provider_8800")
        self.assertNotIn("sensitive", str(raised.exception))

    def test_ineligible_campaign_fails_before_remote_creation(self) -> None:
        campaigns = (
            {
                "Id": 6001,
                "State": "ARCHIVED",
                "Status": "ACCEPTED",
                "Type": "TEXT_CAMPAIGN",
            },
            {
                "Id": 6001,
                "State": "OFF",
                "Status": "ACCEPTED",
                "Type": "UNIFIED_CAMPAIGN",
            },
            {
                "Id": 6001,
                "State": "ON",
                "Status": "DRAFT",
                "Type": "UNIFIED_CAMPAIGN",
            },
            {
                "Id": 6001,
                "State": "ON",
                "Status": "ACCEPTED",
                "Type": "CPM_BANNER_CAMPAIGN",
            },
        )
        for campaign in campaigns:
            with self.subTest(campaign=campaign):
                transport = FakeTransport(
                    [(200, {}, {"result": {"Campaigns": [campaign]}})]
                )
                with self.assertRaises(YandexDirectError):
                    _publish(_provider(transport))
                self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
