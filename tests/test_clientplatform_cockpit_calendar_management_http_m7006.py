from __future__ import annotations

import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None

if _AIOHTTP_AVAILABLE:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from clientplatform.application.cockpit_calendar_management import (
        CockpitCalendarManagementSnapshot,
        CockpitCalendarOffering,
    )
    from clientplatform.domain.bookings import BookingInvariantViolation
    from clientplatform.domain.tenancy import TenantPermissionDenied
    from clientplatform.runtime import cockpit_http
    from clientplatform.runtime.telegram_webapp_auth import TelegramWebAppPrincipal

_TOKEN = "unit-test-token"
_BUSINESS = "11111111-1111-4111-8111-111111111111"


@unittest.skipUnless(_AIOHTTP_AVAILABLE, "aiohttp runtime dependency is not installed")
class CockpitCalendarManagementHttpM7006Tests(unittest.IsolatedAsyncioTestCase):
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

    async def test_management_reauthenticates_and_uses_verified_scope(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        snapshot = CockpitCalendarManagementSnapshot(
            schema_version="2026-09-06.v1",
            business_id=_BUSINESS,
            business_name="Практика",
            timezone_name="Europe/Moscow",
            offerings=(CockpitCalendarOffering(id="off-1", title="Консультация"),),
        )
        calls: list[dict[str, object]] = []

        def resolve(**kwargs: object):
            calls.append(dict(kwargs))
            return snapshot

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "resolve_cockpit_calendar_management", side_effect=resolve),
        ):
            status, payload, headers = await self._post(
                "/clientplatform/cockpit/calendar/manage",
                {"init_data": "verified", "business_id": _BUSINESS, "role": "owner"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["timezone_name"], "Europe/Moscow")
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(calls, [{"telegram_user_id": 101, "requested_business_id": _BUSINESS}])

    async def test_create_passes_only_server_verified_scope_and_booking_fields(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=202, auth_date=1, query_id=None)
        slot = SimpleNamespace(slot=SimpleNamespace(id="slot-1"))
        calls: list[dict[str, object]] = []

        def create(**kwargs: object):
            calls.append(dict(kwargs))
            return slot

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "create_cockpit_calendar_slot", side_effect=create),
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/calendar/create",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "offering_id": "off-1",
                    "local_start": "10.09.2026 15:00",
                    "duration_minutes": 60,
                    "status": "booked",
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["slot_id"], "slot-1")
        self.assertEqual(calls, [{
            "telegram_user_id": 202,
            "requested_business_id": _BUSINESS,
            "offering_id": "off-1",
            "local_start": "10.09.2026 15:00",
            "duration_minutes": 60,
        }])

    async def test_boolean_duration_is_rejected_before_mutation(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=202, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "create_cockpit_calendar_slot") as create,
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/calendar/create",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "offering_id": "off-1",
                    "local_start": "10.09.2026 15:00",
                    "duration_minutes": True,
                },
            )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_calendar_change")
        create.assert_not_called()

    async def test_permission_change_fails_closed_for_management(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(
                cockpit_http,
                "resolve_cockpit_calendar_management",
                side_effect=TenantPermissionDenied("changed"),
            ),
        ):
            status, payload, _ = await self._post(
                "/clientplatform/cockpit/calendar/manage",
                {"init_data": "verified", "business_id": _BUSINESS},
            )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "calendar_manage_denied")

    async def test_booked_or_stale_owner_transition_is_conflict_not_silent_success(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(
                cockpit_http,
                "cancel_cockpit_calendar_slot",
                side_effect=BookingInvariantViolation("booked"),
            ),
        ):
            status, payload, _ = await self._post(
                "/clientplatform/cockpit/calendar/cancel",
                {"init_data": "verified", "business_id": _BUSINESS, "slot_id": "slot-1"},
            )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "calendar_change_rejected")

    async def test_replace_and_cancel_route_only_expected_fields(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=303, auth_date=1, query_id=None)
        replacement = SimpleNamespace(slot=SimpleNamespace(id="slot-new"))
        cancelled = SimpleNamespace(slot=SimpleNamespace(id="slot-old"))
        replace_calls: list[dict[str, object]] = []
        cancel_calls: list[dict[str, object]] = []

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "replace_cockpit_calendar_slot", side_effect=lambda **kwargs: (replace_calls.append(dict(kwargs)) or replacement)),
            patch.object(cockpit_http, "cancel_cockpit_calendar_slot", side_effect=lambda **kwargs: (cancel_calls.append(dict(kwargs)) or cancelled)),
        ):
            replace_status, replace_payload, _ = await self._post(
                "/clientplatform/cockpit/calendar/replace",
                {
                    "init_data": "verified", "business_id": _BUSINESS,
                    "slot_id": "slot-old", "local_start": "11.09.2026 16:00",
                    "duration_minutes": 90, "offering_id": "ignore-me",
                },
            )
            cancel_status, cancel_payload, _ = await self._post(
                "/clientplatform/cockpit/calendar/cancel",
                {"init_data": "verified", "business_id": _BUSINESS, "slot_id": "slot-old", "force": True},
            )
        self.assertEqual((replace_status, replace_payload["slot_id"]), (200, "slot-new"))
        self.assertEqual((cancel_status, cancel_payload["slot_id"]), (200, "slot-old"))
        self.assertEqual(replace_calls, [{
            "telegram_user_id": 303, "requested_business_id": _BUSINESS,
            "slot_id": "slot-old", "local_start": "11.09.2026 16:00", "duration_minutes": 90,
        }])
        self.assertEqual(cancel_calls, [{
            "telegram_user_id": 303, "requested_business_id": _BUSINESS, "slot_id": "slot-old",
        }])

    async def test_shell_keeps_native_management_and_canonical_fallback(self) -> None:
        app = web.Application()
        cockpit_http.register_cockpit_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            shell_response = await client.get("/clientplatform/cockpit")
            shell = await shell_response.text()
            script_response = await client.get("/clientplatform/cockpit/calendar.js")
            script = await script_response.text()
        finally:
            await client.close()
        self.assertEqual(shell_response.status, 200)
        self.assertEqual(script_response.status, 200)
        self.assertIn('id="calendar-manage-panel"', shell)
        self.assertIn('id="calendar-advanced"', shell)
        self.assertIn("/clientplatform/cockpit/calendar/create", script)
        self.assertIn("/clientplatform/cockpit/calendar/replace", script)
        self.assertIn("/clientplatform/cockpit/calendar/cancel", script)
        self.assertIn('openCanonicalSection("calendar", advanced)', script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()
