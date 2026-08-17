from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

from clientplatform.domain.ad_connections import AdConnectionInvariantViolation
from clientplatform.integrations.yandex_direct import YandexOAuthConfig
from clientplatform.integrations.yandex_screen_code import (
    YANDEX_SCREEN_CODE_REDIRECT_URI,
    YandexScreenCodeDirectProvider,
)
from handlers import clientplatform_yandex_screen_code as screen_code
from runtime import ad_oauth_http
from scripts import clientplatform_prepare_production_env as prepare_env


OFFICIAL_STYLE_CODE = "ugsr5hvmusf7zyaf"


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
        encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return status, response_headers, encoded


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


class YandexScreenCodeProviderTests(unittest.TestCase):
    def test_token_exchange_matches_official_screen_code_contract(self) -> None:
        transport = FakeTransport(
            [
                (
                    200,
                    {},
                    {
                        "access_token": "access-token",
                        "token_type": "bearer",
                        "expires_in": 3600,
                        "refresh_token": "refresh-token",
                        "scope": "direct:api",
                    },
                )
            ]
        )
        provider = YandexScreenCodeDirectProvider(
            oauth=YandexOAuthConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri=YANDEX_SCREEN_CODE_REDIRECT_URI,
            ),
            transport=transport,
        )
        bundle = provider.exchange_code(
            code=OFFICIAL_STYLE_CODE,
            verifier="v" * 64,
        )

        self.assertEqual(bundle.access_token, "access-token")
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://oauth.yandex.ru/token")
        form = parse_qs(call["body"].decode("ascii"))
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(form["code"], [OFFICIAL_STYLE_CODE])
        self.assertEqual(form["client_id"], ["client-id"])
        self.assertEqual(form["client_secret"], ["client-secret"])
        self.assertEqual(form["code_verifier"], ["v" * 64])
        self.assertNotIn("redirect_uri", form)

    def test_provider_accepts_opaque_code_and_rejects_invalid_envelope(self) -> None:
        with self.assertRaisesRegex(ValueError, "redirect URI"):
            YandexScreenCodeDirectProvider(
                oauth=YandexOAuthConfig(
                    client_id="client-id",
                    redirect_uri="https://clientplatform.ru/callback",
                )
            )
        transport = FakeTransport(
            [
                (200, {}, {"access_token": "opaque-token", "token_type": "bearer"}),
                (200, {}, {"access_token": "unicode-token", "token_type": "bearer"}),
            ]
        )
        provider = YandexScreenCodeDirectProvider(
            oauth=YandexOAuthConfig(
                client_id="client-id",
                redirect_uri=YANDEX_SCREEN_CODE_REDIRECT_URI,
            ),
            transport=transport,
        )
        with self.assertRaisesRegex(RuntimeError, "oauth_code_invalid"):
            provider.exchange_code(code="", verifier="v" * 64)
        self.assertEqual(transport.calls, [])

        for code, expected_token in (
            ("two words", "opaque-token"),
            ("яндекс", "unicode-token"),
        ):
            bundle = provider.exchange_code(code=code, verifier="v" * 64)
            self.assertEqual(bundle.access_token, expected_token)
            form = parse_qs(transport.calls[-1]["body"].decode("ascii"))
            self.assertEqual(form["code"], [code])


class YandexScreenCodeTelegramTests(unittest.IsolatedAsyncioTestCase):
    def test_state_and_confirmation_code_accept_current_opaque_format(self) -> None:
        self.assertEqual(
            screen_code._oauth_state_from_authorization_url(
                "https://oauth.yandex.ru/authorize?client_id=one&state=safe-state"
            ),
            "safe-state",
        )
        self.assertEqual(
            screen_code._confirmation_code(f"  {OFFICIAL_STYLE_CODE}  "),
            OFFICIAL_STYLE_CODE,
        )
        self.assertEqual(
            screen_code._confirmation_code(f"# {OFFICIAL_STYLE_CODE}"),
            OFFICIAL_STYLE_CODE,
        )
        self.assertEqual(screen_code._confirmation_code("not a code"), "not a code")
        self.assertEqual(screen_code._confirmation_code("яндекс🙂"), "яндекс🙂")
        with self.assertRaises(ValueError):
            screen_code._oauth_state_from_authorization_url(
                "https://oauth.yandex.ru/authorize?client_id=one"
            )
        with self.assertRaises(ValueError):
            screen_code._confirmation_code("")

    def test_ad_connection_failure_reason_maps_all_safe_ownership_states(self) -> None:
        cases = (
            (
                "direct_account_owned_by_another_business",
                "уже подключён к другому рабочему пространству",
            ),
            (
                "direct_identity_reverification_pending",
                "безопасная переверификация",
            ),
            (
                "direct_identity_reverification_ambiguous",
                "несколько старых подключений",
            ),
            (
                "direct_identity_reverification_required",
                "подтвердить заново",
            ),
            (
                "unexpected_internal_code",
                "Код мог истечь или уже быть использован",
            ),
        )
        for code, expected in cases:
            with self.subTest(code=code):
                rendered = screen_code._ad_connection_failure_reason(
                    AdConnectionInvariantViolation(code)
                )
                self.assertIn(expected, rendered)
                self.assertNotIn(code, rendered)

    async def test_connect_stores_one_time_state_and_explains_manual_code(self) -> None:
        cb = callback("cpa:connect:business-1")
        state = FakeState()
        outbound = SimpleNamespace(answer=AsyncMock())
        provider = object()
        start = SimpleNamespace(
            authorization_url=(
                "https://oauth.yandex.ru/authorize?response_type=code"
                "&client_id=client-id&state=oauth-state"
            )
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
            ) as start_oauth,
            patch.object(screen_code, "_message", return_value=outbound),
        ):
            await screen_code.connect_yandex_direct_screen_code(cb, state)

        start_oauth.assert_called_once_with(actor="actor", provider=provider)
        self.assertEqual(state.state, screen_code.YandexScreenCodeState.waiting_code)
        self.assertEqual(state.data["oauth_state"], "oauth-state")
        self.assertEqual(state.data["oauth_user_id"], 101)
        rendered = outbound.answer.await_args.args[0]
        self.assertIn("код подтверждения", rendered)
        self.assertNotIn("семизнач", rendered)
        self.assertIn("удалить", rendered)
        markup = outbound.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].url, start.authorization_url)

    async def test_completion_rejects_empty_envelope_and_sanitizes_provider_failure(self) -> None:
        state = FakeState(
            {
                "business_token": "business-1",
                "oauth_state": "oauth-state",
                "oauth_user_id": 101,
            }
        )
        empty = message("   ")
        with patch.object(screen_code.control, "_user_id", return_value=101):
            await screen_code.complete_yandex_direct_screen_code(empty, state)
        self.assertFalse(state.cleared)
        empty.delete.assert_not_awaited()
        self.assertIn("только код подтверждения", empty.answer.await_args.args[0])

        failed = message("not a code")
        provider = object()
        with (
            patch.object(screen_code.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(screen_code.control, "_user_id", return_value=101),
            patch.object(
                screen_code,
                "screen_code_provider_from_environment",
                return_value=provider,
            ),
            patch.object(
                screen_code,
                "complete_yandex_direct_oauth",
                side_effect=RuntimeError("secret provider response"),
            ) as complete,
        ):
            await screen_code.complete_yandex_direct_screen_code(failed, state)
        complete.assert_called_once_with(
            state="oauth-state",
            code="not a code",
            provider=provider,
        )
        failed.delete.assert_awaited_once()
        self.assertTrue(state.cleared)
        rendered = failed.answer.await_args.args[0]
        self.assertIn("Начните подключение", rendered)
        self.assertNotIn("secret", rendered)

    async def test_completion_explains_cross_tenant_ownership_and_clears_fsm(self) -> None:
        state = FakeState(
            {
                "business_token": "business-1",
                "oauth_state": "oauth-state",
                "oauth_user_id": 101,
            }
        )
        incoming = message(OFFICIAL_STYLE_CODE)
        with (
            patch.object(screen_code.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(screen_code.control, "_user_id", return_value=101),
            patch.object(
                screen_code,
                "screen_code_provider_from_environment",
                return_value=object(),
            ),
            patch.object(
                screen_code,
                "complete_yandex_direct_oauth",
                side_effect=AdConnectionInvariantViolation(
                    "direct_account_owned_by_another_business"
                ),
            ),
        ):
            await screen_code.complete_yandex_direct_screen_code(incoming, state)

        incoming.delete.assert_awaited_once()
        self.assertTrue(state.cleared)
        rendered = incoming.answer.await_args.args[0]
        self.assertIn("уже подключён к другому рабочему пространству", rendered)
        self.assertIn("полностью отзовите подключение", rendered)
        self.assertNotIn("direct_account_owned_by_another_business", rendered)

    async def test_completion_saves_connection_and_returns_to_workspace(self) -> None:
        state = FakeState(
            {
                "business_token": "business-1",
                "oauth_state": "oauth-state",
                "oauth_user_id": 101,
            }
        )
        incoming = message(OFFICIAL_STYLE_CODE)
        provider = object()
        completion = SimpleNamespace(
            connection=SimpleNamespace(external_login="direct-login")
        )
        with (
            patch.object(screen_code.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(screen_code.control, "_user_id", return_value=101),
            patch.object(screen_code.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(
                screen_code,
                "screen_code_provider_from_environment",
                return_value=provider,
            ),
            patch.object(
                screen_code,
                "complete_yandex_direct_oauth",
                return_value=completion,
            ) as complete,
        ):
            await screen_code.complete_yandex_direct_screen_code(incoming, state)

        incoming.delete.assert_awaited_once()
        complete.assert_called_once_with(
            state="oauth-state",
            code=OFFICIAL_STYLE_CODE,
            provider=provider,
        )
        self.assertTrue(state.cleared)
        self.assertIn("direct-login", incoming.answer.await_args.args[0])
        self.assertEqual(
            incoming.answer.await_args.kwargs["reply_markup"],
            [[("Вернуться к рекламным кабинетам", "cpa:home:business-1")]],
        )


class YandexScreenCodeConfigurationTests(unittest.TestCase):
    def test_production_env_accepts_only_official_screen_code_redirect(self) -> None:
        values = {
            "CLIENTPLATFORM_AD_CONNECTIONS_ENABLED": "1",
            "CLIENTPLATFORM_AD_SPEND_MUTATIONS_ENABLED": "0",
            "CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_ID": "client-id",
            "CLIENTPLATFORM_YANDEX_DIRECT_CLIENT_SECRET": "client-secret",
            "CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI": (
                "https://oauth.yandex.ru/verification_code"
            ),
            "CLIENTPLATFORM_AD_CREDENTIAL_IDENTITY_FILE": (
                "/run/secrets/clientplatform-ad/identity.txt"
            ),
            "CLIENTPLATFORM_AD_CREDENTIAL_HOST_DIR": (
                "/var/lib/clientplatform/ad-secrets"
            ),
            "CLIENTPLATFORM_YANDEX_DIRECT_REPORT_TIMEZONE": "UTC",
        }
        prepare_env._validate_ad_connections(values)
        values["CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI"] = (
            "https://clientplatform.ru/oauth/yandex-direct/callback"
        )
        with self.assertRaisesRegex(
            prepare_env.EnvironmentPreparationError,
            "mismatched_clientplatform_ad_oauth_redirect_uri",
        ):
            prepare_env._validate_ad_connections(values)

    def test_screen_code_configuration_does_not_publish_callback_route(self) -> None:
        with (
            patch.dict(
                ad_oauth_http.os.environ,
                {
                    "CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI": (
                        "https://oauth.yandex.ru/verification_code"
                    )
                },
                clear=False,
            ),
            patch.object(ad_oauth_http, "ad_connections_enabled", return_value=True),
            patch.object(
                ad_oauth_http,
                "yandex_direct_provider_configured",
                return_value=True,
            ),
        ):
            self.assertFalse(ad_oauth_http.ad_oauth_http_enabled())


if __name__ == "__main__":
    unittest.main()
