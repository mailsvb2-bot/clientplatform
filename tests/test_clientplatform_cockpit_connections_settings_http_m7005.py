from __future__ import annotations

import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None

if _AIOHTTP_AVAILABLE:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from clientplatform.application.cockpit_connections import CockpitConnectionsSnapshot
    from clientplatform.application.cockpit_settings import CockpitSettingsSnapshot
    from clientplatform.domain.connections import ConnectionPlatform
    from clientplatform.domain.tenancy import TenantPermissionDenied
    from clientplatform.runtime import cockpit_http
    from clientplatform.runtime.telegram_webapp_auth import TelegramWebAppPrincipal

_TOKEN = "unit-test-token"
_BUSINESS = "11111111-1111-4111-8111-111111111111"


@unittest.skipUnless(_AIOHTTP_AVAILABLE, "aiohttp runtime dependency is not installed")
class CockpitConnectionsSettingsHttpM7005Tests(unittest.IsolatedAsyncioTestCase):
    async def _post(self, path: str, payload: dict[str, object]):
        app = web.Application()
        cockpit_http.register_cockpit_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(path, json=payload)
            body = await response.json()
            headers = dict(response.headers)
        finally:
            await client.close()
        return response.status, body, headers

    async def test_connections_reauthenticates_and_uses_verified_scope(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        snapshot = CockpitConnectionsSnapshot(
            schema_version="2026-09-06.v1", business_id=_BUSINESS, business_name="Практика", items=()
        )
        calls: list[dict[str, object]] = []

        def resolve(**kwargs: object):
            calls.append(dict(kwargs))
            return snapshot

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "resolve_cockpit_connections", side_effect=resolve),
        ):
            status, payload, headers = await self._post(
                "/clientplatform/cockpit/connections", {"init_data": "verified", "business_id": _BUSINESS}
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["business_id"], _BUSINESS)
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(calls, [{"telegram_user_id": 101, "requested_business_id": _BUSINESS}])

    async def test_setup_returns_only_https_one_time_url_after_server_issue(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=202, auth_date=1, query_id=None)
        issued = SimpleNamespace(
            platform=ConnectionPlatform.VK,
            token="A" * 43,
            expires_at="2026-09-06T19:00:00+00:00",
        )
        calls: list[dict[str, object]] = []

        def issue(**kwargs: object):
            calls.append(dict(kwargs))
            return issued

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http.settings, "MESSENGER_PUBLIC_BASE_URL", "https://app.clientplatform.ru"),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "issue_cockpit_messenger_setup", side_effect=issue),
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/connections/setup",
                {"init_data": "verified", "business_id": _BUSINESS, "platform": "vk"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["setup_url"].startswith("https://app.clientplatform.ru/clientplatform/connect/"))
        self.assertNotIn("token", payload)
        self.assertEqual(
            calls,
            [{"telegram_user_id": 202, "requested_business_id": _BUSINESS, "platform": "vk"}],
        )

    async def test_setup_does_not_issue_capability_without_https_public_base(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=202, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http.settings, "MESSENGER_PUBLIC_BASE_URL", "http://unsafe.example"),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "issue_cockpit_messenger_setup") as issue,
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/connections/setup",
                {"init_data": "verified", "business_id": _BUSINESS, "platform": "vk"},
            )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "connections_unavailable")
        issue.assert_not_called()

    async def test_settings_update_passes_only_verified_identity_and_submitted_fields(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=303, auth_date=1, query_id=None)
        snapshot = CockpitSettingsSnapshot(
            schema_version="2026-09-06.v1",
            business_id=_BUSINESS,
            business_name="Новая практика",
            activity_description="Консультации",
            timezone_name="Europe/Moscow",
        )
        calls: list[dict[str, object]] = []

        def update(**kwargs: object):
            calls.append(dict(kwargs))
            return snapshot

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "update_cockpit_settings", side_effect=update),
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/settings/update",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "business_name": "Новая практика",
                    "activity_description": "Консультации",
                    "timezone_name": "Europe/Moscow",
                    "role": "owner",
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["business_name"], "Новая практика")
        self.assertEqual(
            calls,
            [{
                "telegram_user_id": 303,
                "requested_business_id": _BUSINESS,
                "business_name": "Новая практика",
                "activity_description": "Консультации",
                "timezone_name": "Europe/Moscow",
            }],
        )

    async def test_permission_change_fails_closed_for_native_management(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "resolve_cockpit_connections", side_effect=TenantPermissionDenied("changed")),
        ):
            status, payload, _ = await self._post(
                "/clientplatform/cockpit/connections", {"init_data": "verified", "business_id": _BUSINESS}
            )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "connections_access_denied")

    async def test_shell_serves_both_native_management_assets(self) -> None:
        app = web.Application()
        cockpit_http.register_cockpit_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            shell_response = await client.get("/clientplatform/cockpit")
            shell = await shell_response.text()
            connections_response = await client.get("/clientplatform/cockpit/connections.js")
            connections_script = await connections_response.text()
            settings_response = await client.get("/clientplatform/cockpit/settings.js")
            settings_script = await settings_response.text()
        finally:
            await client.close()
        self.assertEqual(shell_response.status, 200)
        self.assertEqual(connections_response.status, 200)
        self.assertEqual(settings_response.status, 200)
        self.assertIn('id="connections-view"', shell)
        self.assertIn('id="settings-view"', shell)
        self.assertIn("/clientplatform/cockpit/connections/setup", connections_script)
        self.assertIn("/clientplatform/cockpit/settings/update", settings_script)
        self.assertNotIn("localStorage", connections_script + settings_script)
        self.assertNotIn("innerHTML", connections_script + settings_script)


if __name__ == "__main__":
    unittest.main()
