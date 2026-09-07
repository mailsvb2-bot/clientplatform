from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import patch

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None

if _AIOHTTP_AVAILABLE:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from clientplatform.application.cockpit_sales_management import CockpitSalesLeadManagement
    from clientplatform.domain.sales import SalesInvariantViolation, SalesLeadNotFound
    from clientplatform.domain.tenancy import TenantPermissionDenied
    from clientplatform.runtime import cockpit_http
    from clientplatform.runtime.telegram_webapp_auth import TelegramWebAppAuthError, TelegramWebAppPrincipal

_TOKEN = "unit-test-token"
_BUSINESS = "11111111-1111-4111-8111-111111111111"
_LEAD = "22222222-2222-4222-8222-222222222222"
_CUSTOMER = "33333333-3333-4333-8333-333333333333"


def _detail(*, stage: str = "qualified"):
    return CockpitSalesLeadManagement(
        schema_version="2026-09-07.v1",
        business_id=_BUSINESS,
        business_name="Практика",
        timezone_name="Europe/Moscow",
        lead_id=_LEAD,
        customer_id=_CUSTOMER,
        customer_name="Анна",
        stage=stage,
        stage_label="Интерес подтверждён" if stage == "qualified" else stage,
        assigned=True,
        assigned_to_me=True,
        next_action="Позвонить" if stage not in {"won", "lost"} else None,
        due_at="2026-09-10T12:30:00+00:00" if stage not in {"won", "lost"} else None,
        due_local_value="2026-09-10T15:30" if stage not in {"won", "lost"} else None,
        closure_reason="нет бюджета" if stage == "lost" else None,
        source_kind="vk",
        closed=stage in {"won", "lost"},
        can_reopen=stage == "lost",
    )


@unittest.skipUnless(_AIOHTTP_AVAILABLE, "aiohttp runtime dependency is not installed")
class CockpitSalesManagementHttpM7007Tests(unittest.IsolatedAsyncioTestCase):
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

    def _auth(self, user_id: int = 101):
        return (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(
                cockpit_http,
                "verify_telegram_webapp_init_data",
                return_value=TelegramWebAppPrincipal(user_id=user_id, auth_date=1, query_id=None),
            ),
        )

    async def test_manage_uses_only_verified_user_and_selected_business(self) -> None:
        calls: list[dict[str, object]] = []

        def resolve(**kwargs: object):
            calls.append(dict(kwargs))
            return _detail()

        auth_token, auth_verify = self._auth(202)
        with (
            auth_token,
            auth_verify,
            patch.object(cockpit_http, "resolve_cockpit_sales_lead", side_effect=resolve),
        ):
            status, payload, headers = await self._post(
                "/clientplatform/cockpit/sales/manage",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "lead_id": _LEAD,
                    "user_id": 999,
                    "role": "owner",
                    "membership_id": "spoofed",
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["lead_id"], _LEAD)
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(
            calls,
            [{"telegram_user_id": 202, "requested_business_id": _BUSINESS, "lead_id": _LEAD}],
        )

    async def test_assignment_does_not_accept_client_side_assignee(self) -> None:
        calls: list[dict[str, object]] = []

        def assign(**kwargs: object):
            calls.append(dict(kwargs))
            return _detail()

        auth_token, auth_verify = self._auth(303)
        with (
            auth_token,
            auth_verify,
            patch.object(cockpit_http, "assign_cockpit_sales_lead", side_effect=assign),
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/sales/assignment",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "lead_id": _LEAD,
                    "action": "assign",
                    "assigned_user_id": 999,
                    "assigned_member_id": "spoofed",
                },
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["assigned_to_me"])
        self.assertEqual(
            calls,
            [{"telegram_user_id": 303, "requested_business_id": _BUSINESS, "lead_id": _LEAD}],
        )

    async def test_stage_passes_only_canonical_fields_and_requires_valid_shape(self) -> None:
        calls: list[dict[str, object]] = []

        def stage(**kwargs: object):
            calls.append(dict(kwargs))
            return _detail(stage="lost")

        auth_token, auth_verify = self._auth()
        with (
            auth_token,
            auth_verify,
            patch.object(cockpit_http, "set_cockpit_sales_stage", side_effect=stage),
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/sales/stage",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "lead_id": _LEAD,
                    "stage": "lost",
                    "reason": "нет бюджета",
                    "closure_reason": "spoofed",
                },
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["closed"])
        self.assertEqual(
            calls,
            [{
                "telegram_user_id": 101,
                "requested_business_id": _BUSINESS,
                "lead_id": _LEAD,
                "stage": "lost",
                "reason": "нет бюджета",
            }],
        )

        auth_token, auth_verify = self._auth()
        with auth_token, auth_verify:
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/sales/stage",
                {"init_data": "verified", "business_id": _BUSINESS, "lead_id": _LEAD, "stage": 1},
            )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_sales_change")

    async def test_next_action_passes_business_local_value_not_browser_offset_authority(self) -> None:
        calls: list[dict[str, object]] = []

        def change(**kwargs: object):
            calls.append(dict(kwargs))
            return _detail()

        auth_token, auth_verify = self._auth()
        with (
            auth_token,
            auth_verify,
            patch.object(cockpit_http, "set_cockpit_sales_next_action", side_effect=change),
        ):
            status, _payload, _headers = await self._post(
                "/clientplatform/cockpit/sales/next-action",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "lead_id": _LEAD,
                    "next_action": "Отправить предложение",
                    "due_local": "2026-09-10T15:30",
                    "timezone": "Pacific/Honolulu",
                    "due_at": "2099-01-01T00:00:00Z",
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            calls,
            [{
                "telegram_user_id": 101,
                "requested_business_id": _BUSINESS,
                "lead_id": _LEAD,
                "next_action": "Отправить предложение",
                "due_local": "2026-09-10T15:30",
            }],
        )

    async def test_note_requires_idempotency_key_and_forwards_no_spoofed_actor(self) -> None:
        calls: list[dict[str, object]] = []

        def add(**kwargs: object):
            calls.append(dict(kwargs))
            return _detail()

        auth_token, auth_verify = self._auth(404)
        with (
            auth_token,
            auth_verify,
            patch.object(cockpit_http, "add_cockpit_sales_note", side_effect=add),
        ):
            status, _payload, _headers = await self._post(
                "/clientplatform/cockpit/sales/note",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "lead_id": _LEAD,
                    "note": "Позвонить после 18:00",
                    "interaction_key": "event-1",
                    "actor": "spoofed",
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            calls,
            [{
                "telegram_user_id": 404,
                "requested_business_id": _BUSINESS,
                "lead_id": _LEAD,
                "note": "Позвонить после 18:00",
                "interaction_key": "event-1",
            }],
        )

        auth_token, auth_verify = self._auth()
        with auth_token, auth_verify:
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/sales/note",
                {"init_data": "verified", "business_id": _BUSINESS, "lead_id": _LEAD, "note": "x"},
            )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_sales_change")

    async def test_domain_and_permission_errors_fail_closed(self) -> None:
        cases = [
            (TenantPermissionDenied("no"), 403, "sales_manage_denied"),
            (SalesLeadNotFound("gone"), 404, "sales_lead_not_found"),
            (SalesInvariantViolation("stale"), 409, "sales_change_rejected"),
            (ValueError("bad"), 400, "invalid_sales_change"),
        ]
        for error, expected_status, expected_code in cases:
            with self.subTest(code=expected_code):
                auth_token, auth_verify = self._auth()
                with (
                    auth_token,
                    auth_verify,
                    patch.object(cockpit_http, "resolve_cockpit_sales_lead", side_effect=error),
                ):
                    status, payload, _headers = await self._post(
                        "/clientplatform/cockpit/sales/manage",
                        {"init_data": "verified", "business_id": _BUSINESS, "lead_id": _LEAD},
                    )
                self.assertEqual(status, expected_status)
                self.assertEqual(payload["error"], expected_code)

    async def test_unverified_init_data_never_reaches_sales_operation(self) -> None:
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", side_effect=TelegramWebAppAuthError("bad auth")),
            patch.object(cockpit_http, "resolve_cockpit_sales_lead") as resolve,
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/sales/manage",
                {"init_data": "bad", "business_id": _BUSINESS, "lead_id": _LEAD},
            )
        self.assertNotEqual(status, 200)
        self.assertFalse(payload["ok"])
        resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
