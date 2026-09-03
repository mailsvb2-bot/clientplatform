from __future__ import annotations

import importlib.util

import pytest

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None
pytestmark = pytest.mark.skipif(not _AIOHTTP_AVAILABLE, reason="aiohttp runtime dependency is not installed")

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


@pytest.mark.asyncio
async def test_cockpit_shell_is_mobile_safe_and_contains_no_tenant_authority() -> None:
    app = web.Application()
    cockpit_http.register_cockpit_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/clientplatform/cockpit?business_id=forged")
        body = await response.text()
        script_response = await client.get("/clientplatform/cockpit/app.js")
        script = await script_response.text()
    finally:
        await client.close()
    assert response.status == 200
    assert "viewport-fit=cover" in body
    assert "https://telegram.org/js/telegram-web-app.js" in body
    assert "Content-Security-Policy" in response.headers
    assert "business_id=forged" not in body
    assert "URLSearchParams" not in script
    assert "localStorage" not in script
    assert "innerHTML" not in script
    assert "payload.navigation" in script


@pytest.mark.asyncio
async def test_cockpit_context_authenticates_then_uses_server_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cockpit_http.settings, "BOT_TOKEN", _TOKEN)
    monkeypatch.setattr(
        cockpit_http,
        "verify_telegram_webapp_init_data",
        lambda *_args, **_kwargs: TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None),
    )
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

    def resolve(*, telegram_user_id: int, requested_business_id: str | None = None):
        calls.append((telegram_user_id, requested_business_id))
        return resolved

    monkeypatch.setattr(cockpit_http, "resolve_cockpit_context", resolve)
    app = web.Application(middlewares=[native_messenger_http_admission.native_messenger_http_admission_middleware])
    cockpit_http.register_cockpit_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/clientplatform/cockpit/context",
            json={"init_data": "verified-by-test", "business_id": _BUSINESS_A},
        )
        payload = await response.json()
    finally:
        await client.close()
    assert response.status == 200
    assert payload["user_id"] == 202
    assert payload["business_id"] == _BUSINESS_A
    assert calls == [(101, _BUSINESS_A)]
    assert response.headers["Cache-Control"] == "no-store, max-age=0"


@pytest.mark.asyncio
async def test_cockpit_http_fails_closed_for_invalid_identity_and_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cockpit_http.settings, "BOT_TOKEN", _TOKEN)
    app = web.Application()
    cockpit_http.register_cockpit_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        invalid = await client.post("/clientplatform/cockpit/context", json={"init_data": "not-signed"})
        assert invalid.status == 401

        monkeypatch.setattr(
            cockpit_http,
            "verify_telegram_webapp_init_data",
            lambda *_args, **_kwargs: TelegramWebAppPrincipal(user_id=101, auth_date=1, query_id=None),
        )
        monkeypatch.setattr(
            cockpit_http,
            "resolve_cockpit_context",
            lambda **_kwargs: (_ for _ in ()).throw(TenantAccessDenied("denied")),
        )
        denied = await client.post(
            "/clientplatform/cockpit/context",
            json={"init_data": "verified-by-test", "business_id": _BUSINESS_B},
        )
        assert denied.status == 403
        assert (await denied.json())["error"] == "business_access_denied"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cockpit_admission_rejects_oversized_body_before_handler() -> None:
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

    response = await native_messenger_http_admission.native_messenger_http_admission_middleware(
        request,  # type: ignore[arg-type]
        handler,  # type: ignore[arg-type]
    )
    assert response.status == 413
    assert called is False
    assert request.reads == 0
