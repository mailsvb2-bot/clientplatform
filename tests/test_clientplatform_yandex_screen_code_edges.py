from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

from clientplatform.integrations.yandex_direct import (
    YandexDirectError,
    YandexOAuthConfig,
)
from clientplatform.integrations.yandex_screen_code import (
    YANDEX_SCREEN_CODE_REDIRECT_URI,
    YandexScreenCodeDirectProvider,
    screen_code_provider_from_environment,
)
from handlers import clientplatform_yandex_screen_code as screen_code


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


class FakeTransport:
    def __init__(self, status: int, payload: object):
        self.status = status
        self.payload = payload
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
        encoded = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode("utf-8")
        return self.status, {}, encoded


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
    )


class YandexScreenCodeProviderEdgeTests(unittest.TestCase):
    def test_environment_factory_fails_closed_for_each_missing_setting(self) -> None:
        base = {
            "CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID": "client-id",
            "CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET": "client-secret",
            "CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI": YANDEX_SCREEN_CODE_REDIRECT_URI,
        }
        for missing in tuple(base):
            values = dict(base)
            values[missing] = ""
            with (
                self.subTest(missing=missing),
                patch.dict(os.environ, values, clear=False),
                self.assertRaises(RuntimeError),
            ):
                screen_code_provider_from_environment()

        with patch.dict(os.environ, base, clear=False):
            provider = screen_code_provider_from_environment()
        self.assertIsInstance(provider, YandexScreenCodeDirectProvider)

    def test_authorization_url_uses_official_host_redirect_state_and_pkce(self) -> None:
        provider = YandexScreenCodeDirectProvider(
            oauth=YandexOAuthConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri=YANDEX_SCREEN_CODE_REDIRECT_URI,
            ),
            transport=FakeTransport(200, {}),
        )
        authorization_url = provider.authorization_url(
            state="state-value",
            verifier="v" * 64,
        )
        parsed = urlparse(authorization_url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "oauth.yandex.ru")
        self.assertEqual(query["redirect_uri"], [YANDEX_SCREEN_CODE_REDIRECT_URI])
        self.assertEqual(query["state"], ["state-value"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("v" * 64, authorization_url)

    def test_token_exchange_handles_list_scope_and_missing_access_token(self) -> None:
        provider = YandexScreenCodeDirectProvider(
            oauth=YandexOAuthConfig(
                client_id="client-id",
                redirect_uri=YANDEX_SCREEN_CODE_REDIRECT_URI,
            ),
            transport=FakeTransport(
                200,
                {
                    "access_token": "access-token",
                    "scope": ["direct:api"],
                    "expires_in": None,
                },
            ),
        )
        bundle = provider.exchange_code(code="1234567", verifier="v" * 64)
        self.assertEqual(bundle.scope, ("direct:api",))
        self.assertIsNone(bundle.expires_in)
        self.assertIsNone(bundle.refresh_token)

        missing = YandexScreenCodeDirectProvider(
            oauth=YandexOAuthConfig(
                client_id="client-id",
                redirect_uri=YANDEX_SCREEN_CODE_REDIRECT_URI,
            ),
            transport=FakeTransport(200, {"scope": "direct:api"}),
        )
        with self.assertRaisesRegex(YandexDirectError, "oauth_access_token_missing"):
            missing.exchange_code(code="1234567", verifier="v" * 64)


class YandexScreenCodeTelegramEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_failure_is_sanitized_and_does_not_create_fsm_state(self) -> None:
        cb = callback("cpa:connect:business-1")
        state = FakeState()
        with (
            patch.object(screen_code.control, "_token_uuid", return_value="business-id"),
            patch.object(
                screen_code.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(
                screen_code,
                "screen_code_provider_from_environment",
                side_effect=RuntimeError("secret configuration detail"),
            ),
        ):
            await screen_code.connect_yandex_direct_screen_code(cb, state)
        cb.answer.assert_awaited_once_with(
            "Не удалось начать подключение",
            show_alert=True,
        )
        self.assertIsNone(state.state)
        self.assertEqual(state.data, {})

    async def test_valid_code_from_wrong_user_is_not_exchanged(self) -> None:
        incoming = message("1234567")
        state = FakeState(
            {
                "business_token": "business-1",
                "oauth_state": "oauth-state",
                "oauth_user_id": 101,
            }
        )
        with (
            patch.object(screen_code.control, "_user_id", return_value=202),
            patch.object(
                screen_code,
                "screen_code_provider_from_environment",
            ) as provider_factory,
            patch.object(
                screen_code,
                "complete_yandex_direct_oauth",
                new=Mock(),
            ) as complete,
        ):
            await screen_code.complete_yandex_direct_screen_code(incoming, state)
        provider_factory.assert_not_called()
        complete.assert_not_called()
        self.assertTrue(state.cleared)
        self.assertIn("другому пользователю", incoming.answer.await_args.args[0])
        self.assertIn("Начните подключение", incoming.answer.await_args.args[0])

    async def test_missing_fsm_session_is_cleared_before_provider_call(self) -> None:
        incoming = message("1234567")
        state = FakeState({"business_token": "business-1"})
        with patch.object(
            screen_code,
            "screen_code_provider_from_environment",
        ) as provider_factory:
            await screen_code.complete_yandex_direct_screen_code(incoming, state)
        provider_factory.assert_not_called()
        self.assertTrue(state.cleared)
        self.assertIn("Сессия подключения потеряна", incoming.answer.await_args.args[0])

    async def test_invalid_authorization_url_is_sanitized(self) -> None:
        cb = callback("cpa:connect:business-1")
        state = FakeState()
        provider = object()
        start = SimpleNamespace(
            authorization_url="https://oauth.yandex.ru/authorize?client_id=client-id"
        )
        with (
            patch.object(screen_code.asyncio, "to_thread", new=immediate_to_thread),
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
            ),
            patch.object(
                screen_code,
                "start_yandex_direct_oauth",
                return_value=start,
            ),
        ):
            await screen_code.connect_yandex_direct_screen_code(cb, state)
        cb.answer.assert_awaited_once_with(
            "Не удалось начать подключение",
            show_alert=True,
        )
        self.assertIsNone(state.state)


if __name__ == "__main__":
    unittest.main()
