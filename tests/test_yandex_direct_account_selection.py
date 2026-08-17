from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from clientplatform.domain.ad_connections import pkce_challenge
from clientplatform.integrations.yandex_direct import YandexDirectError, YandexOAuthConfig
from clientplatform.integrations.yandex_screen_code import (
    YANDEX_SCREEN_CODE_REDIRECT_URI,
    YandexScreenCodeDirectProvider,
    normalize_yandex_login_hint,
)
from handlers import clientplatform_yandex_screen_code as screen_code


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict[str, str], object]]) -> None:
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
        encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return status, response_headers, encoded


class FakeState:
    def __init__(self, data=None) -> None:
        self.data = dict(data or {})
        self.state = None
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def set_data(self, data):
        self.data = dict(data)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.cleared = True
        self.data.clear()


async def immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def callback(data: str):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def message(text: str):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        delete=AsyncMock(),
    )


class YandexDirectAccountSelectionProviderTests(unittest.TestCase):
    def _provider(self, *, transport=None, login_hint=None):
        return YandexScreenCodeDirectProvider(
            oauth=YandexOAuthConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri=YANDEX_SCREEN_CODE_REDIRECT_URI,
            ),
            transport=transport,
            login_hint=login_hint,
        )

    def test_login_hint_targets_requested_yandex_account_without_weakening_oauth(self) -> None:
        verifier = "v" * 64
        provider = self._provider(login_hint="wanted-account@yandex.ru")
        url = provider.authorization_url(state="safe-state", verifier=verifier)
        query = parse_qs(urlparse(url).query)

        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["client-id"])
        self.assertEqual(query["redirect_uri"], [YANDEX_SCREEN_CODE_REDIRECT_URI])
        self.assertEqual(query["force_confirm"], ["yes"])
        self.assertEqual(query["state"], ["safe-state"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], [pkce_challenge(verifier)])
        self.assertEqual(query["login_hint"], ["wanted-account@yandex.ru"])

    def test_login_hint_is_absent_for_normal_account_picker(self) -> None:
        url = self._provider().authorization_url(state="safe-state", verifier="v" * 64)
        query = parse_qs(urlparse(url).query)
        self.assertNotIn("login_hint", query)
        self.assertEqual(query["force_confirm"], ["yes"])

    def test_login_hint_boundary_is_bounded_and_single_value(self) -> None:
        self.assertEqual(normalize_yandex_login_hint("  wanted-login  "), "wanted-login")
        for value in (None, "", "   ", "two accounts", "a" * 321):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(YandexDirectError, "oauth_login_hint_invalid"):
                    normalize_yandex_login_hint(value)

    def test_direct_identity_comes_from_clients_get_not_generic_yandex_id(self) -> None:
        transport = FakeTransport(
            [
                (
                    200,
                    {},
                    {
                        "result": {
                            "Clients": [
                                {"ClientId": 123456789, "Login": "direct-owner"}
                            ]
                        }
                    },
                )
            ]
        )
        provider = self._provider(transport=transport)

        identity = provider.account_identity(access_token="opaque-access-token")

        self.assertEqual(identity.account_id, "123456789")
        self.assertEqual(identity.login, "direct-owner")
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://api.direct.yandex.com/json/v5/clients")
        self.assertEqual(call["headers"]["Authorization"], "Bearer opaque-access-token")
        payload = json.loads(call["body"].decode("utf-8"))
        self.assertEqual(payload["method"], "get")
        self.assertEqual(payload["params"]["FieldNames"], ["ClientId", "Login"])
        self.assertNotIn("login.yandex.ru", str(call["url"]))

    def test_direct_identity_failure_is_stage_namespaced_and_fail_closed(self) -> None:
        transport = FakeTransport(
            [
                (
                    200,
                    {},
                    {"error": {"error_code": 53, "error_string": "Authorization error"}},
                )
            ]
        )
        provider = self._provider(transport=transport)

        with self.assertRaises(YandexDirectError) as raised:
            provider.account_identity(access_token="opaque-access-token")

        self.assertTrue(raised.exception.code.startswith("direct_identity_"))

    def test_ambiguous_or_missing_direct_identity_is_never_auto_selected(self) -> None:
        for payload in (
            {"result": {"Clients": []}},
            {
                "result": {
                    "Clients": [
                        {"ClientId": 1, "Login": "one"},
                        {"ClientId": 2, "Login": "two"},
                    ]
                }
            },
            {"result": {"Clients": [{"ClientId": "", "Login": "missing"}]}},
        ):
            with self.subTest(payload=payload):
                provider = self._provider(transport=FakeTransport([(200, {}, payload)]))
                with self.assertRaises(YandexDirectError):
                    provider.account_identity(access_token="opaque-access-token")


class YandexDirectAccountSelectionTelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_mine_first_asks_how_to_select_yandex_account(self) -> None:
        cb = callback("cpa:connect-mine:business-token")
        state = FakeState({"stale": "value"})
        outbound = SimpleNamespace(answer=AsyncMock())
        with (
            patch.object(screen_code.control, "_token_uuid", return_value="business-id"),
            patch.object(
                screen_code.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(screen_code, "_message", return_value=outbound),
            patch.object(screen_code, "start_yandex_direct_oauth") as start_oauth,
        ):
            await screen_code.choose_yandex_account_mode(cb, state)

        self.assertTrue(state.cleared)
        start_oauth.assert_not_called()
        rendered = outbound.answer.await_args.args[0]
        self.assertIn("несколько аккаунтов", rendered)
        markup = outbound.answer.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("cpa:yandex-account-auto:business-token", callbacks)
        self.assertIn("cpa:yandex-account-hint:business-token", callbacks)

    async def test_explicit_login_hint_starts_targeted_oauth_and_is_not_kept_in_fsm(self) -> None:
        state = FakeState(
            {
                "business_id": "business-id",
                "business_token": "business-token",
                "oauth_user_id": 101,
            }
        )
        incoming = message("wanted-account@yandex.ru")
        provider = object()
        start = SimpleNamespace(
            authorization_url=(
                "https://oauth.yandex.ru/authorize?response_type=code"
                "&client_id=client-id&state=oauth-state&login_hint=wanted-account%40yandex.ru"
            )
        )
        with (
            patch.object(screen_code.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(screen_code.control, "_user_id", return_value=101),
            patch.object(screen_code.control, "_token_uuid", return_value="business-id"),
            patch.object(
                screen_code.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(
                screen_code,
                "screen_code_provider_from_environment",
                return_value=provider,
            ) as provider_factory,
            patch.object(
                screen_code,
                "start_yandex_direct_oauth",
                return_value=start,
            ) as start_oauth,
        ):
            await screen_code.receive_yandex_account_hint(incoming, state)

        provider_factory.assert_called_once_with(login_hint="wanted-account@yandex.ru")
        start_oauth.assert_called_once_with(actor="actor", provider=provider)
        self.assertEqual(state.state, screen_code.YandexScreenCodeState.waiting_code)
        self.assertEqual(state.data["oauth_state"], "oauth-state")
        self.assertNotIn("login_hint", state.data)
        incoming.delete.assert_awaited_once()
        rendered = incoming.answer.await_args.args[0]
        self.assertIn("именно указанный", rendered)

    def test_provider_failure_is_classified_without_exposing_provider_code(self) -> None:
        direct_exc = YandexDirectError("direct_identity_provider_error_53")
        oauth_exc = YandexDirectError("oauth_invalid_grant")

        self.assertEqual(screen_code._provider_failure_stage(direct_exc), "direct_identity")
        self.assertEqual(screen_code._provider_failure_stage(oauth_exc), "token_exchange")
        direct_reason = screen_code._provider_failure_reason(direct_exc)
        oauth_reason = screen_code._provider_failure_reason(oauth_exc)
        self.assertIn("Яндекс Директа", direct_reason)
        self.assertIn("Яндекс OAuth", oauth_reason)
        self.assertNotIn(direct_exc.code, direct_reason)
        self.assertNotIn(oauth_exc.code, oauth_reason)

    def test_callback_payloads_fit_telegram_limit_for_uuid_business_tokens(self) -> None:
        token = "12345678-1234-1234-1234-123456789abc"
        for prefix in (
            "cpa:yandex-account-auto:",
            "cpa:yandex-account-hint:",
            "cpa:yandex-cancel:",
        ):
            with self.subTest(prefix=prefix):
                self.assertLessEqual(len((prefix + token).encode("utf-8")), 64)


if __name__ == "__main__":
    unittest.main()
