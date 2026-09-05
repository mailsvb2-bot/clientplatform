from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import patch

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None

if _AIOHTTP_AVAILABLE:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from clientplatform.application import cockpit_customers
    from clientplatform.domain.customers import CustomerNotFound
    from clientplatform.runtime import cockpit_http
    from clientplatform.runtime.telegram_webapp_auth import TelegramWebAppPrincipal

_TOKEN = "unit-test-token"
_BUSINESS = "11111111-1111-4111-8111-111111111111"
_CUSTOMER = "33333333-3333-4333-8333-333333333333"


@unittest.skipUnless(_AIOHTTP_AVAILABLE, "aiohttp runtime dependency is not installed")
class CockpitCustomersHttpM7003Tests(unittest.IsolatedAsyncioTestCase):
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

    async def test_customer_page_reauthenticates_and_passes_bounded_selectors(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        page = cockpit_customers.CockpitCustomerPage(
            schema_version="2026-09-05.v1",
            business_id=_BUSINESS,
            role="owner",
            query="Анна",
            limit=10,
            offset=20,
            has_more=True,
            next_offset=30,
            previous_offset=10,
            items=(),
        )
        calls: list[dict[str, object]] = []

        def resolve(**kwargs: object):
            calls.append(dict(kwargs))
            return page

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(
                cockpit_http,
                "verify_telegram_webapp_init_data",
                return_value=principal,
            ),
            patch.object(cockpit_http, "resolve_cockpit_customer_page", side_effect=resolve),
        ):
            status, payload, headers = await self._post(
                "/clientplatform/cockpit/customers",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "query": "Анна",
                    "limit": 10,
                    "offset": 20,
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["next_offset"], 30)
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(calls[0]["telegram_user_id"], 101)
        self.assertEqual(calls[0]["requested_business_id"], _BUSINESS)
        self.assertEqual(calls[0]["query"], "Анна")
        self.assertEqual(calls[0]["limit"], 10)
        self.assertEqual(calls[0]["offset"], 20)

    async def test_customer_detail_uses_internal_selector_and_never_mutates(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        detail = cockpit_customers.CockpitCustomerDetail(
            schema_version="2026-09-05.v1",
            business_id=_BUSINESS,
            role="support",
            customer_id=_CUSTOMER,
            display_name="Анна",
            status="active",
            created_at="2026-09-01T10:00:00+00:00",
            updated_at="2026-09-05T10:00:00+00:00",
            contacts=(),
            timeline=(),
            next_action=None,
            limitations=(),
        )
        calls: list[dict[str, object]] = []

        def resolve(**kwargs: object):
            calls.append(dict(kwargs))
            return detail

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(
                cockpit_http,
                "verify_telegram_webapp_init_data",
                return_value=principal,
            ),
            patch.object(cockpit_http, "resolve_cockpit_customer_detail", side_effect=resolve),
        ):
            status, payload, headers = await self._post(
                "/clientplatform/cockpit/customers/detail",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "customer_id": _CUSTOMER,
                    "timeline_limit": 20,
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["customer_id"], _CUSTOMER)
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(calls[0]["customer_id"], _CUSTOMER)
        self.assertEqual(calls[0]["timeline_limit"], 20)

    async def test_forged_customer_is_not_leaked(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(
                cockpit_http,
                "verify_telegram_webapp_init_data",
                return_value=principal,
            ),
            patch.object(
                cockpit_http,
                "resolve_cockpit_customer_detail",
                side_effect=CustomerNotFound("not in tenant"),
            ),
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/customers/detail",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "customer_id": _CUSTOMER,
                },
            )
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"ok": False, "error": "customer_not_found"})

    async def test_invalid_unbounded_page_is_rejected(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(
                cockpit_http,
                "verify_telegram_webapp_init_data",
                return_value=principal,
            ),
            patch.object(
                cockpit_http,
                "resolve_cockpit_customer_page",
                side_effect=ValueError("limit"),
            ),
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/customers",
                {"init_data": "verified", "limit": 5000},
            )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_customer_request")


if __name__ == "__main__":
    unittest.main()
