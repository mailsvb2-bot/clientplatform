from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import patch

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None

if _AIOHTTP_AVAILABLE:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from clientplatform.application import cockpit_calendar, cockpit_sales
    from clientplatform.domain.tenancy import TenantAccessDenied, TenantPermissionDenied
    from clientplatform.runtime import cockpit_http
    from clientplatform.runtime.telegram_webapp_auth import TelegramWebAppPrincipal

_TOKEN = "unit-test-token"
_BUSINESS = "11111111-1111-4111-8111-111111111111"


@unittest.skipUnless(_AIOHTTP_AVAILABLE, "aiohttp runtime dependency is not installed")
class CockpitPrimaryWorkspaceHttpM7004Tests(unittest.IsolatedAsyncioTestCase):
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

    async def test_calendar_reauthenticates_and_passes_only_verified_scope(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        snapshot = cockpit_calendar.CockpitCalendarSnapshot(
            schema_version="2026-09-06.v1",
            business_id=_BUSINESS,
            business_name="Практика",
            timezone_name="Europe/Tallinn",
            as_of="2026-09-06T10:00:00+00:00",
            items=(),
            has_more=False,
        )
        calls: list[dict[str, object]] = []

        def resolve(**kwargs: object):
            calls.append(dict(kwargs))
            return snapshot

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "resolve_cockpit_calendar", side_effect=resolve),
        ):
            status, payload, headers = await self._post(
                "/clientplatform/cockpit/calendar",
                {"init_data": "verified", "business_id": _BUSINESS, "limit": 17},
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["business_id"], _BUSINESS)
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(
            calls,
            [{"telegram_user_id": 101, "requested_business_id": _BUSINESS, "limit": 17}],
        )

    async def test_sales_reauthenticates_and_passes_only_verified_scope(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=202, auth_date=1, query_id=None)
        snapshot = cockpit_sales.CockpitSalesSnapshot(
            schema_version="2026-09-06.v1",
            business_id=_BUSINESS,
            business_name="Практика",
            timezone_name="Europe/Tallinn",
            as_of="2026-09-06T10:00:00+00:00",
            items=(),
            handoff_count=0,
            has_more=False,
        )
        calls: list[dict[str, object]] = []

        def resolve(**kwargs: object):
            calls.append(dict(kwargs))
            return snapshot

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "resolve_cockpit_sales", side_effect=resolve),
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/sales",
                {"init_data": "verified", "business_id": _BUSINESS, "limit": 9},
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["handoff_count"], 0)
        self.assertEqual(
            calls,
            [{"telegram_user_id": 202, "requested_business_id": _BUSINESS, "limit": 9}],
        )

    async def test_native_workspace_routes_fail_closed_after_live_permission_change(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(
                cockpit_http,
                "resolve_cockpit_calendar",
                side_effect=TenantPermissionDenied("changed"),
            ),
        ):
            calendar_status, calendar_payload, _ = await self._post(
                "/clientplatform/cockpit/calendar",
                {"init_data": "verified", "business_id": _BUSINESS},
            )
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(
                cockpit_http,
                "resolve_cockpit_sales",
                side_effect=TenantAccessDenied("changed"),
            ),
        ):
            sales_status, sales_payload, _ = await self._post(
                "/clientplatform/cockpit/sales",
                {"init_data": "verified", "business_id": _BUSINESS},
            )
        self.assertEqual(calendar_status, 403)
        self.assertEqual(calendar_payload["error"], "calendar_access_denied")
        self.assertEqual(sales_status, 403)
        self.assertEqual(sales_payload["error"], "business_access_denied")

    async def test_shell_serves_primary_navigation_and_first_party_native_assets(self) -> None:
        app = web.Application()
        cockpit_http.register_cockpit_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            shell_response = await client.get("/clientplatform/cockpit")
            shell = await shell_response.text()
            calendar_response = await client.get("/clientplatform/cockpit/calendar.js")
            calendar_script = await calendar_response.text()
            sales_response = await client.get("/clientplatform/cockpit/sales.js")
            sales_script = await sales_response.text()
        finally:
            await client.close()
        self.assertEqual(shell_response.status, 200)
        self.assertEqual(calendar_response.status, 200)
        self.assertEqual(sales_response.status, 200)
        self.assertIn('id="primary-nav"', shell)
        self.assertIn("Что сделать сейчас", shell)
        self.assertIn("/clientplatform/cockpit/calendar", calendar_script)
        self.assertIn("/clientplatform/cockpit/sales", sales_script)
        self.assertNotIn("localStorage", calendar_script + sales_script)
        self.assertNotIn("innerHTML", calendar_script + sales_script)


if __name__ == "__main__":
    unittest.main()
