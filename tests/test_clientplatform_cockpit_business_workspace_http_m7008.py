from __future__ import annotations

import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None

if _AIOHTTP_AVAILABLE:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from clientplatform.domain.tenancy import TenantPermissionDenied
    from clientplatform.runtime import cockpit_http
    from clientplatform.runtime.telegram_webapp_auth import TelegramWebAppPrincipal

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_TOKEN = "unit-test-token"


@unittest.skipUnless(_AIOHTTP_AVAILABLE, "aiohttp runtime dependency is not installed")
class CockpitBusinessWorkspaceHttpM7008Tests(unittest.IsolatedAsyncioTestCase):
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

    async def test_service_create_uses_verified_identity_not_browser_role(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=303, auth_date=1, query_id=None)
        calls: list[dict[str, object]] = []

        def create(**kwargs: object):
            calls.append(dict(kwargs))
            return SimpleNamespace(id="44444444-4444-4444-8444-444444444444")

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "create_cockpit_service", side_effect=create),
        ):
            status, payload, headers = await self._post(
                "/clientplatform/cockpit/services/create",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "capability_id": "33333333-3333-4333-8333-333333333333",
                    "title": "Консультация",
                    "description": "60 минут",
                    "user_id": 999,
                    "role": "owner",
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(
            calls,
            [{
                "telegram_user_id": 303,
                "requested_business_id": _BUSINESS,
                "capability_id": "33333333-3333-4333-8333-333333333333",
                "title": "Консультация",
                "description": "60 минут",
            }],
        )
        self.assertNotIn("role", calls[0])
        self.assertNotIn("user_id", calls[0])

    async def test_money_record_passes_only_verified_scope_and_finance_fields(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=404, auth_date=1, query_id=None)
        calls: list[dict[str, object]] = []

        def record(**kwargs: object):
            calls.append(dict(kwargs))
            return SimpleNamespace(id="55555555-5555-4555-8555-555555555555")

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "record_cockpit_payment", side_effect=record),
        ):
            status, _payload, _headers = await self._post(
                "/clientplatform/cockpit/money/record",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "amount": "3500",
                    "currency": "RUB",
                    "request_id": "66666666-6666-4666-8666-666666666666",
                    "customer_id": None,
                    "offering_id": None,
                    "note": "Консультация",
                    "role": "owner",
                    "actor_user_id": 999,
                    "amount_minor": 1,
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            calls,
            [{
                "telegram_user_id": 404,
                "requested_business_id": _BUSINESS,
                "amount": "3500",
                "currency": "RUB",
                "request_id": "66666666-6666-4666-8666-666666666666",
                "customer_id": None,
                "offering_id": None,
                "note": "Консультация",
            }],
        )

    async def test_growth_rechecks_permission_and_fails_closed(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=505, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(
                cockpit_http,
                "resolve_cockpit_growth_analytics",
                side_effect=TenantPermissionDenied("changed"),
            ),
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/growth",
                {"init_data": "verified", "business_id": _BUSINESS, "period_days": 7},
            )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "growth_access_denied")

    async def test_analytics_rejects_unapproved_period_before_application_call(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=505, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "resolve_cockpit_growth_analytics") as resolve,
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/analytics",
                {"init_data": "verified", "business_id": _BUSINESS, "period_days": 14},
            )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_growth_request")
        resolve.assert_not_called()

    async def test_shell_and_first_party_asset_expose_all_four_native_views(self) -> None:
        app = web.Application()
        cockpit_http.register_cockpit_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            shell_response = await client.get("/clientplatform/cockpit")
            shell = await shell_response.text()
            asset_response = await client.get("/clientplatform/cockpit/business-workspace.js")
            script = await asset_response.text()
        finally:
            await client.close()
        self.assertEqual(shell_response.status, 200)
        self.assertEqual(asset_response.status, 200)
        for view_id in ("services-view", "money-view", "growth-view", "analytics-view"):
            self.assertIn(f'id="{view_id}"', shell)
        self.assertIn('/clientplatform/cockpit/services/create', script)
        self.assertIn('/clientplatform/cockpit/money/refund', script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()
