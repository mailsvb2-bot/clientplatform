from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import patch

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None

if _AIOHTTP_AVAILABLE:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from clientplatform.application import cockpit
    from clientplatform.domain.tenancy import TenantAccessDenied
    from clientplatform.runtime import cockpit_http, native_messenger_http_admission
    from clientplatform.runtime.telegram_webapp_auth import TelegramWebAppPrincipal

_TOKEN = "123456:unit-test-token"
_BUSINESS_A = "11111111-1111-4111-8111-111111111111"
_BUSINESS_B = "22222222-2222-4222-8222-222222222222"


@unittest.skipUnless(
    _AIOHTTP_AVAILABLE,
    "aiohttp runtime dependency is not installed in dependency-light Canon",
)
class CockpitHttpM7001Tests(unittest.IsolatedAsyncioTestCase):
    async def test_cockpit_shell_is_mobile_safe_and_contains_no_tenant_authority(
        self,
    ) -> None:
        app = web.Application()
        cockpit_http.register_cockpit_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get(
                "/clientplatform/cockpit?business_id=forged"
            )
            body = await response.text()
            script_response = await client.get("/clientplatform/cockpit/app.js")
            script = await script_response.text()
            styles_response = await client.get("/clientplatform/cockpit/styles.css")
            styles = await styles_response.text()
        finally:
            await client.close()
        self.assertEqual(response.status, 200)
        self.assertIn("viewport-fit=cover", body)
        self.assertIn("https://telegram.org/js/telegram-web-app.js", body)
        self.assertIn("Content-Security-Policy", response.headers)
        self.assertNotIn("business_id=forged", body)
        self.assertNotIn("URLSearchParams", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("innerHTML", script)
        self.assertIn("payload.navigation", script)
        self.assertIn("Главный экран", body)
        self.assertIn("Обновить", body)
        self.assertNotIn("Home / Today", body)
        self.assertIn("tg.BackButton.onClick", script)
        self.assertIn("loadHome().catch(homeFail)", script)
        self.assertIn("Роль: ${roleNames[payload.role]", script)
        self.assertIn("Подробнее:", script)
        self.assertNotIn("payload.timezone_name", script)
        self.assertIn("--tg-theme-bg-color", styles)
        self.assertIn("safe-area-inset-top", styles)
        self.assertIn("safe-area-inset-bottom", styles)

    async def test_cockpit_context_authenticates_then_uses_server_context(
        self,
    ) -> None:
        resolved = cockpit.CockpitContext(
            user_id=202,
            business_id=_BUSINESS_A,
            business_name="Практика",
            role="owner",
            onboarding_required=False,
            businesses=(),
            navigation=(),
        )
        calls: list[tuple[int, str | None]] = []

        def resolve(
            *, telegram_user_id: int, requested_business_id: str | None = None
        ) -> cockpit.CockpitContext:
            calls.append((telegram_user_id, requested_business_id))
            return resolved

        principal = TelegramWebAppPrincipal(
            user_id=101,
            auth_date=1,
            query_id=None,
        )
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(
                cockpit_http,
                "verify_telegram_webapp_init_data",
                return_value=principal,
            ),
            patch.object(
                cockpit_http,
                "resolve_cockpit_context",
                side_effect=resolve,
            ),
        ):
            app = web.Application(
                middlewares=[
                    native_messenger_http_admission.native_messenger_http_admission_middleware
                ]
            )
            cockpit_http.register_cockpit_routes(app)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/clientplatform/cockpit/context",
                    json={
                        "init_data": "verified-by-test",
                        "business_id": _BUSINESS_A,
                    },
                )
                payload = await response.json()
            finally:
                await client.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["user_id"], 202)
        self.assertEqual(payload["business_id"], _BUSINESS_A)
        self.assertEqual(calls, [(101, _BUSINESS_A)])
        self.assertEqual(
            response.headers["Cache-Control"],
            "no-store, max-age=0",
        )

    async def test_cockpit_http_fails_closed_for_invalid_identity_and_tenant(
        self,
    ) -> None:
        with patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN):
            app = web.Application()
            cockpit_http.register_cockpit_routes(app)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                invalid = await client.post(
                    "/clientplatform/cockpit/context",
                    json={"init_data": "not-signed"},
                )
                self.assertEqual(invalid.status, 401)

                principal = TelegramWebAppPrincipal(
                    user_id=101,
                    auth_date=1,
                    query_id=None,
                )
                with (
                    patch.object(
                        cockpit_http,
                        "verify_telegram_webapp_init_data",
                        return_value=principal,
                    ),
                    patch.object(
                        cockpit_http,
                        "resolve_cockpit_context",
                        side_effect=TenantAccessDenied("denied"),
                    ),
                ):
                    denied = await client.post(
                        "/clientplatform/cockpit/context",
                        json={
                            "init_data": "verified-by-test",
                            "business_id": _BUSINESS_B,
                        },
                    )
                    self.assertEqual(denied.status, 403)
                    self.assertEqual(
                        (await denied.json())["error"],
                        "business_access_denied",
                    )
            finally:
                await client.close()

    async def test_cockpit_admission_rejects_oversized_body_before_handler(
        self,
    ) -> None:
        native_messenger_http_admission.reset_native_messenger_http_admission_state_for_tests()

        class Request:
            method = "POST"
            path = "/clientplatform/cockpit/context"
            content_length = 20_000
            reads = 0

            async def read(self) -> bytes:
                self.reads += 1
                return b"{}"

        request = Request()
        called = False

        async def handler(_request: object) -> web.Response:
            nonlocal called
            called = True
            return web.Response(text="unexpected")

        response = (
            await native_messenger_http_admission.native_messenger_http_admission_middleware(
                request,  # type: ignore[arg-type]
                handler,  # type: ignore[arg-type]
            )
        )
        self.assertEqual(response.status, 413)
        self.assertFalse(called)
        self.assertEqual(request.reads, 0)


if __name__ == "__main__":
    unittest.main()
