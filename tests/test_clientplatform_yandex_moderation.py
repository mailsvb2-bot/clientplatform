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
            redirect_uri="https://app.clientplatform.ru/oauth/yandex-direct/callback",
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
        href="https://t.me/clientplatform_bot?start=cpa_source",
        idempotency_key="adjob_0123456789abcdef0123456789abcdef",
    )


class YandexModerationTests(unittest.TestCase):
    def test_new_ad_is_submitted_for_moderation(self) -> None:
        transport = FakeTransport(
            [
                (200, {}, {"result": {"AdGroups": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 7001}]}}),
                (200, {}, {"result": {"Ads": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 8001}]}}),
                (
                    200,
                    {},
                    {"result": {"Ads": [{"Id": 8001, "Status": "DRAFT"}]}},
                ),
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
        self.assertEqual(len(transport.calls), 6)
        moderate = json.loads(transport.calls[-1]["body"])
        self.assertEqual(moderate["method"], "moderate")
        self.assertEqual(
            moderate["params"]["SelectionCriteria"]["Ids"],
            [8001],
        )
        for call in transport.calls:
            self.assertEqual(call["headers"]["Authorization"], "Bearer secret-token")
            self.assertNotIn("secret-token", str(call["body"]))

    def test_existing_draft_is_moderated_without_duplicate_remote_objects(self) -> None:
        transport = FakeTransport(
            [
                (
                    200,
                    {},
                    {
                        "result": {
                            "AdGroups": [
                                {
                                    "Id": 7001,
                                    "Name": "ClientPlatform adjob_0123456789abcdef0123456789abcdef",
                                }
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
                                    "TextAd": {
                                        "Href": "https://t.me/clientplatform_bot?start=cpa_source"
                                    },
                                }
                            ]
                        }
                    },
                ),
                (
                    200,
                    {},
                    {"result": {"Ads": [{"Id": 8001, "Status": "DRAFT"}]}},
                ),
                (
                    200,
                    {},
                    {"result": {"ModerateResults": [{"Id": 8001}]}},
                ),
            ]
        )
        result = _publish(_provider(transport))
        self.assertEqual((result.ad_group_id, result.ad_id), ("7001", "8001"))
        methods = [json.loads(call["body"])["method"] for call in transport.calls]
        self.assertEqual(methods, ["get", "get", "get", "moderate"])

    def test_reviewing_or_accepted_ad_is_not_moderated_again(self) -> None:
        for status in ("MODERATION", "PREACCEPTED", "ACCEPTED"):
            with self.subTest(status=status):
                transport = FakeTransport(
                    [
                        (
                            200,
                            {},
                            {
                                "result": {
                                    "AdGroups": [
                                        {
                                            "Id": 7001,
                                            "Name": "ClientPlatform adjob_0123456789abcdef0123456789abcdef",
                                        }
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
                                            "TextAd": {
                                                "Href": "https://t.me/clientplatform_bot?start=cpa_source"
                                            },
                                        }
                                    ]
                                }
                            },
                        ),
                        (
                            200,
                            {},
                            {
                                "result": {
                                    "Ads": [{"Id": 8001, "Status": status}]
                                }
                            },
                        ),
                    ]
                )
                _publish(_provider(transport))
                methods = [
                    json.loads(call["body"])["method"] for call in transport.calls
                ]
                self.assertEqual(methods, ["get", "get", "get"])

    def test_rejected_ad_requires_manual_review_instead_of_resubmission(self) -> None:
        transport = FakeTransport(
            [
                (
                    200,
                    {},
                    {
                        "result": {
                            "AdGroups": [
                                {
                                    "Id": 7001,
                                    "Name": "ClientPlatform adjob_0123456789abcdef0123456789abcdef",
                                }
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
                                    "TextAd": {
                                        "Href": "https://t.me/clientplatform_bot?start=cpa_source"
                                    },
                                }
                            ]
                        }
                    },
                ),
                (
                    200,
                    {},
                    {"result": {"Ads": [{"Id": 8001, "Status": "REJECTED"}]}},
                ),
            ]
        )
        with self.assertRaises(YandexDirectError) as raised:
            _publish(_provider(transport))
        self.assertEqual(raised.exception.code, "ad_rejected_requires_manual_review")
        self.assertEqual(len(transport.calls), 3)

    def test_moderation_error_is_reduced_to_safe_provider_code(self) -> None:
        transport = FakeTransport(
            [
                (200, {}, {"result": {"AdGroups": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 7001}]}}),
                (200, {}, {"result": {"Ads": []}}),
                (200, {}, {"result": {"AddResults": [{"Id": 8001}]}}),
                (
                    200,
                    {},
                    {"result": {"Ads": [{"Id": 8001, "Status": "DRAFT"}]}},
                ),
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
                                            "Message": "sensitive provider detail",
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


if __name__ == "__main__":
    unittest.main()
