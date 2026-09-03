from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import patch

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None

if _AIOHTTP_AVAILABLE:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from clientplatform.application import cockpit_home
    from clientplatform.domain.tenancy import TenantAccessDenied
    from clientplatform.runtime import cockpit_http, native_messenger_http_admission
    from clientplatform.runtime.telegram_webapp_auth import TelegramWebAppPrincipal

_TOKEN = "123456:unit-test-token"
_BUSINESS = "11111111-1111-4111-8111-111111111111"


@unittest.skipUnless(
    _AIOHTTP_AVAILABLE,
    "aiohttp runtime dependency is not installed in dependency-light Canon",
)
class CockpitHomeHttpM7002Tests(unittest.IsolatedAsyncioTestCase):
    async def test_home_authenticates_and_returns_bounded_server_projection(self) -> None:
        snapshot = cockpit_home.CockpitHomeSnapshot(
            schema_version="2026-09-04.v1",
            business_id=_BUSINESS,
            business_name="Практика",
            role="owner",
            timezone_name="Europe/Tallinn",
            as_of="2026-09-03T21:30:00+00:00",
            today_from="2026-09-03T21:00:00+00:00",
            today_to="2026-09-04T21:00:00+00:00",
            metrics=(),
            money=(),
            attention=(),
            actions=(),
            limitations=(),
            empty_message="Ничего срочного.",
        )
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        calls: list[tuple[int, str | None]] = []

        def resolve(*, telegram_user_id: int, requested_business_id: str | None = None) -> cockpit_home.CockpitHomeSnapshot:
            calls.append((telegram_user_id, requested_business_id))
            return snapshot

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "resolve_cockpit_home", side_effect=resolve),
        ):
            app = web.Application(middlewares=[native_messenger_http_admission.native_messenger_http_admission_middleware])
            cockpit_http.register_cockpit_routes(app)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/clientplatform/cockpit/home",
                    json={"init_data": "verified-by-test", "business_id": _BUSINESS},
                )
                payload = await response.json()
            finally:
                await client.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["schema_version"], "2026-09-04.v1")
        self.assertEqual(payload["business_id"], _BUSINESS)
        self.assertEqual(calls, [(101, _BUSINESS)])
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")

    async def test_home_fails_closed_for_forged_business(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "resolve_cockpit_home", side_effect=TenantAccessDenied("denied")),
        ):
            app = web.Application()
            cockpit_http.register_cockpit_routes(app)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/clientplatform/cockpit/home",
                    json={"init_data": "verified", "business_id": "22222222-2222-4222-8222-222222222222"},
                )
                payload = await response.json()
            finally:
                await client.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(payload["error"], "business_access_denied")

    async def test_home_admission_rejects_oversized_body_before_handler(self) -> None:
        native_messenger_http_admission.reset_native_messenger_http_admission_state_for_tests()

        class Request:
            method = "POST"
            path = "/clientplatform/cockpit/home"
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

        response = await native_messenger_http_admission.native_messenger_http_admission_middleware(
            request,  # type: ignore[arg-type]
            handler,  # type: ignore[arg-type]
        )
        self.assertEqual(response.status, 413)
        self.assertFalse(called)
        self.assertEqual(request.reads, 0)

    async def test_shell_script_uses_home_api_without_client_authority_or_mutation(self) -> None:
        app = web.Application()
        cockpit_http.register_cockpit_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            shell_response = await client.get("/clientplatform/cockpit")
            shell = await shell_response.text()
            script_response = await client.get("/clientplatform/cockpit/app.js")
            script = await script_response.text()
        finally:
            await client.close()
        self.assertEqual(shell_response.status, 200)
        self.assertIn("Требует внимания", shell)
        self.assertIn("Что сделать дальше", shell)
        self.assertIn("/clientplatform/cockpit/home", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("URLSearchParams", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("/approve", script)
        self.assertNotIn("/send", script)


if __name__ == "__main__":
    unittest.main()
