from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import patch

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None

if _AIOHTTP_AVAILABLE:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from clientplatform.application.cockpit_automation import CockpitAutomationSnapshot
    from clientplatform.domain.automation_policy import AutomationApprovalConflict
    from clientplatform.domain.tenancy import TenantPermissionDenied
    from clientplatform.runtime import cockpit_http
    from clientplatform.runtime.telegram_webapp_auth import TelegramWebAppPrincipal

_TOKEN = "unit-test-token"
_BUSINESS = "11111111-1111-4111-8111-111111111111"
_APPROVAL = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_FINGERPRINT = "a" * 64


def _snapshot() -> "CockpitAutomationSnapshot":
    return CockpitAutomationSnapshot(
        schema_version="2026-09-14.v1",
        business_id=_BUSINESS,
        business_name="Практика",
        mode="normal",
        mode_label="Обычный",
        policy_state="Согласованная политика действует",
        policy_expires_at="01.10.2026 15:00",
        autopilot_enabled=False,
        can_change_policy=True,
        pending_count=0,
        approvals=(),
        safety_note="Решение не запускает внешнее действие.",
    )


@unittest.skipUnless(_AIOHTTP_AVAILABLE, "aiohttp runtime dependency is not installed")
class CockpitAutomationHttpM7009Tests(unittest.IsolatedAsyncioTestCase):
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

    async def test_projection_uses_verified_telegram_identity_and_business_scope(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None)
        calls: list[dict[str, object]] = []

        def resolve(**kwargs: object):
            calls.append(dict(kwargs))
            return _snapshot()

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "resolve_cockpit_automation", side_effect=resolve),
        ):
            status, payload, headers = await self._post(
                "/clientplatform/cockpit/automation",
                {"init_data": "verified", "business_id": _BUSINESS, "role": "owner"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["business_id"], _BUSINESS)
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(
            calls,
            [{"telegram_user_id": 101, "requested_business_id": _BUSINESS}],
        )

    async def test_decision_passes_exact_browser_fingerprint_to_canonical_owner(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=202, auth_date=1, query_id=None)
        calls: list[dict[str, object]] = []

        def decide(**kwargs: object):
            calls.append(dict(kwargs))
            return _snapshot()

        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(cockpit_http, "decide_cockpit_automation_approval", side_effect=decide),
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/automation/decision",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "approval_id": _APPROVAL,
                    "request_fingerprint": _FINGERPRINT,
                    "decision": "approve",
                    "user_id": 999999,
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["business_id"], _BUSINESS)
        self.assertEqual(
            calls,
            [{
                "telegram_user_id": 202,
                "requested_business_id": _BUSINESS,
                "approval_id": _APPROVAL,
                "request_fingerprint": _FINGERPRINT,
                "decision": "approve",
            }],
        )

    async def test_stale_decision_conflict_is_fail_closed(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=202, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(
                cockpit_http,
                "decide_cockpit_automation_approval",
                side_effect=AutomationApprovalConflict("changed"),
            ),
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/automation/decision",
                {
                    "init_data": "verified",
                    "business_id": _BUSINESS,
                    "approval_id": _APPROVAL,
                    "request_fingerprint": _FINGERPRINT,
                    "decision": "approve",
                },
            )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "automation_change_rejected")

    async def test_owner_boundary_change_is_403_not_frontend_authority(self) -> None:
        principal = TelegramWebAppPrincipal(user_id=303, auth_date=1, query_id=None)
        with (
            patch.object(cockpit_http.settings, "BOT_TOKEN", _TOKEN),
            patch.object(cockpit_http, "verify_telegram_webapp_init_data", return_value=principal),
            patch.object(
                cockpit_http,
                "set_cockpit_autopilot",
                side_effect=TenantPermissionDenied("owner only"),
            ),
        ):
            status, payload, _headers = await self._post(
                "/clientplatform/cockpit/automation/autopilot",
                {"init_data": "verified", "business_id": _BUSINESS, "enabled": True},
            )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "automation_change_denied")

    async def test_shell_serves_native_automation_without_browser_policy_store(self) -> None:
        app = web.Application()
        cockpit_http.register_cockpit_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            shell_response = await client.get("/clientplatform/cockpit")
            shell = await shell_response.text()
            script_response = await client.get("/clientplatform/cockpit/automation.js")
            script = await script_response.text()
        finally:
            await client.close()
        self.assertEqual(shell_response.status, 200)
        self.assertEqual(script_response.status, 200)
        self.assertIn('id="automation-view"', shell)
        self.assertIn('src="/clientplatform/cockpit/automation.js"', shell)
        self.assertIn("/clientplatform/cockpit/automation/decision", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("policy_hash", script)


if __name__ == "__main__":
    unittest.main()
