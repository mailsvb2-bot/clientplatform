from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path

from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexOAuthConfig,
)
from clientplatform.integrations.yandex_direct_moderation import (
    ModeratingYandexDirectProvider,
)
from clientplatform.integrations.yandex_oauth_lifecycle import YandexOAuthLifecycle


class FakeTransport:
    def __init__(self, responses=None, *, raised: Exception | None = None):
        self.responses = list(responses or [])
        self.raised = raised
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
        if self.raised is not None:
            raise self.raised
        status, response_headers, payload = self.responses.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return status, response_headers, raw


def _provider(transport: FakeTransport) -> ModeratingYandexDirectProvider:
    return ModeratingYandexDirectProvider(
        oauth=YandexOAuthConfig(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://app.clientplatform.ru/oauth/yandex-direct/callback",
        ),
        transport=transport,
    )


class DirectIdentitySafetyTests(unittest.TestCase):
    def test_identity_comes_from_clients_service_and_requires_edit_grant(self) -> None:
        transport = FakeTransport(
            [
                (
                    200,
                    {},
                    {
                        "result": {
                            "Clients": [
                                {
                                    "ClientId": 100500,
                                    "Login": "master-login",
                                    "Type": "CLIENT",
                                    "ClientInfo": "Мастер",
                                    "Archived": "NO",
                                    "Grants": [
                                        {
                                            "Privilege": "EDIT_CAMPAIGNS",
                                            "Value": "YES",
                                        },
                                        {
                                            "Privilege": "IMPORT_XLS",
                                            "Value": "YES",
                                        },
                                    ],
                                }
                            ]
                        }
                    },
                )
            ]
        )

        identity = _provider(transport).account_identity(
            access_token="private-token"
        )

        self.assertEqual(identity.account_id, "100500")
        self.assertEqual(identity.login, "master-login")
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(str(transport.calls[0]["url"]).endswith("/clients"))
        request = json.loads(transport.calls[0]["body"])
        self.assertEqual(request["method"], "get")
        self.assertIn("Type", request["params"]["FieldNames"])
        self.assertIn("Grants", request["params"]["FieldNames"])

    def test_denied_edit_grant_is_rejected_before_activation(self) -> None:
        transport = FakeTransport(
            [
                (
                    200,
                    {},
                    {
                        "result": {
                            "Clients": [
                                {
                                    "ClientId": 100500,
                                    "Login": "denied-login",
                                    "Type": "CLIENT",
                                    "Archived": "NO",
                                    "Grants": [
                                        {
                                            "Privilege": "EDIT_CAMPAIGNS",
                                            "Value": "NO",
                                        },
                                        {
                                            "Privilege": "IMPORT_XLS",
                                            "Value": "YES",
                                        },
                                    ],
                                }
                            ]
                        }
                    },
                )
            ]
        )

        with self.assertRaises(YandexDirectError) as raised:
            _provider(transport).account_identity(access_token="private-token")

        self.assertEqual(raised.exception.code, "direct_account_is_read_only")

    def test_read_only_direct_account_is_rejected_before_activation(self) -> None:
        transport = FakeTransport(
            [
                (
                    200,
                    {},
                    {
                        "result": {
                            "Clients": [
                                {
                                    "ClientId": 100500,
                                    "Login": "readonly-login",
                                    "Type": "CLIENT",
                                    "Archived": "NO",
                                    "Grants": [],
                                }
                            ]
                        }
                    },
                )
            ]
        )

        with self.assertRaises(YandexDirectError) as raised:
            _provider(transport).account_identity(access_token="private-token")

        self.assertEqual(raised.exception.code, "direct_account_is_read_only")

    def test_ambiguous_direct_identity_is_rejected(self) -> None:
        transport = FakeTransport(
            [(200, {}, {"result": {"Clients": []}})]
        )

        with self.assertRaises(YandexDirectError) as raised:
            _provider(transport).account_identity(access_token="private-token")

        self.assertEqual(
            raised.exception.code,
            "direct_account_identity_ambiguous",
        )


class DisconnectPrivacySafetyTests(unittest.TestCase):
    def test_transport_failure_never_blocks_local_credential_erasure(self) -> None:
        lifecycle = YandexOAuthLifecycle(
            client_id="client-id",
            client_secret="client-secret",
            transport=FakeTransport(
                raised=YandexDirectError(
                    "provider_transport_unavailable",
                    retryable=True,
                )
            ),
        )

        result = lifecycle.revoke(access_token="private-token")

        self.assertFalse(result.provider_revoked)
        self.assertTrue(result.local_erasure_allowed)
        self.assertEqual(
            result.provider_error_code,
            "provider_transport_unavailable",
        )

    def test_invalid_provider_response_never_blocks_local_erasure(self) -> None:
        lifecycle = YandexOAuthLifecycle(
            client_id="client-id",
            client_secret="client-secret",
            transport=FakeTransport([(502, {}, b"not-json")]),
        )

        result = lifecycle.revoke(access_token="private-token")

        self.assertFalse(result.provider_revoked)
        self.assertTrue(result.local_erasure_allowed)
        self.assertEqual(
            result.provider_error_code,
            "oauth_revoke_response_invalid",
        )


class NoAutomaticSpendStaticContractTests(unittest.TestCase):
    def test_provider_source_contains_no_activation_or_targeting_calls(self) -> None:
        source = inspect.getsource(ModeratingYandexDirectProvider)
        forbidden_moderate = '"method"' + ': "moderate"'
        forbidden_keyword_add = 'service=' + '"keywords"'
        self.assertNotIn(forbidden_moderate, source)
        self.assertNotIn("_moderate_ad", source)
        self.assertNotIn(forbidden_keyword_add, source)
        self.assertNotIn("_ensure_keyword", source)
        self.assertNotIn("_resume_keyword", source)

    def test_user_confirmation_is_described_as_draft_not_launch(self) -> None:
        source_path = (
            Path(__file__).resolve().parents[1]
            / "handlers"
            / "clientplatform_ad_connections.py"
        )
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("Создать черновик в Яндекс Директе", source)
        self.assertIn("расходы автоматически не запускаются", source)
        self.assertNotIn("Отправить в Яндекс Директ", source)


if __name__ == "__main__":
    unittest.main()
