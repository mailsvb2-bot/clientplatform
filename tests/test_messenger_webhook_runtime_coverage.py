from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from runtime import messenger_webhooks


class FakeRouter:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Any]] = []

    def add_get(self, path: str, handler: Any) -> None:
        self.routes.append(("GET", path, handler))

    def add_post(self, path: str, handler: Any) -> None:
        self.routes.append(("POST", path, handler))


class FakeApplication(dict):
    instances: list["FakeApplication"] = []

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.kwargs = kwargs
        self.router = FakeRouter()
        self.instances.append(self)


class FakeRunner:
    instances: list["FakeRunner"] = []
    cleanup_fail: BaseException | None = None

    def __init__(self, app: Any) -> None:
        self.app = app
        self.setup_calls = 0
        self.cleanup_calls = 0
        self.instances.append(self)

    async def setup(self) -> None:
        self.setup_calls += 1

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        if self.cleanup_fail is not None:
            raise self.cleanup_fail


class FakeSite:
    instances: list["FakeSite"] = []
    fail: BaseException | None = None

    def __init__(self, runner: Any, *, host: str, port: int) -> None:
        self.runner = runner
        self.host = host
        self.port = port
        self.start_calls = 0
        self.instances.append(self)

    async def start(self) -> None:
        self.start_calls += 1
        if self.fail is not None:
            raise self.fail


class FakeRequest:
    def __init__(self, body: str = "", headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}
        self.cloned_headers: dict[str, str] | None = None

    async def text(self) -> str:
        return self._body

    def clone(self, *, headers: dict[str, str]) -> "FakeRequest":
        clone = FakeRequest(self._body, dict(headers))
        self.cloned_headers = dict(headers)
        return clone


class FakeGatewayRuntime:
    instances: list["FakeGatewayRuntime"] = []
    start_result = True
    stop_error: BaseException | None = None

    def __init__(self, *, dispatcher: Any, config: Any) -> None:
        self.dispatcher = dispatcher
        self.config = config
        self.registered_apps: list[Any] = []
        self.start_calls = 0
        self.stop_calls = 0
        self.instances.append(self)

    def register_route(self, app: Any) -> None:
        self.registered_apps.append(app)
        app["clientplatform_bot_gateway_runtime"] = self

    def start(self) -> bool:
        self.start_calls += 1
        return self.start_result

    async def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error

    def health_snapshot(self) -> dict[str, Any]:
        return {"running": True, "transport": "polling", "active_pollers": 2}


def _reset_fakes() -> None:
    FakeApplication.instances.clear()
    FakeRunner.instances.clear()
    FakeRunner.cleanup_fail = None
    FakeSite.instances.clear()
    FakeSite.fail = None
    FakeGatewayRuntime.instances.clear()
    FakeGatewayRuntime.start_result = True
    FakeGatewayRuntime.stop_error = None


def _patch_runtime_surface(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payment: bool = False,
    privacy: bool = False,
    max_enabled: bool = False,
    vk_enabled: bool = False,
    ingress: bool = True,
    gateway: bool = False,
) -> None:
    _reset_fakes()
    monkeypatch.setattr(messenger_webhooks.web, "Application", FakeApplication)
    monkeypatch.setattr(messenger_webhooks.web, "AppRunner", FakeRunner)
    monkeypatch.setattr(messenger_webhooks.web, "TCPSite", FakeSite)
    monkeypatch.setattr(
        messenger_webhooks,
        "ManagedBotGatewayRuntime",
        FakeGatewayRuntime,
    )
    monkeypatch.setattr(messenger_webhooks, "payment_http_enabled", lambda: payment)
    monkeypatch.setattr(
        messenger_webhooks,
        "privacy_export_http_enabled",
        lambda: privacy,
    )
    monkeypatch.setattr(
        messenger_webhooks,
        "max_webhook_enabled",
        lambda: max_enabled,
    )
    monkeypatch.setattr(
        messenger_webhooks,
        "vk_webhook_enabled",
        lambda: vk_enabled,
    )
    monkeypatch.setattr(messenger_webhooks, "http_ingress_enabled", lambda: ingress)
    monkeypatch.setattr(
        messenger_webhooks,
        "bot_gateway_runtime_config",
        lambda: SimpleNamespace(enabled=gateway),
    )
    monkeypatch.setattr(messenger_webhooks, "ingress_body_limit", lambda: 262_144)
    monkeypatch.setattr(
        messenger_webhooks,
        "settings",
        SimpleNamespace(
            APP_ENV="test",
            VK_GROUP_ID="10",
            MESSENGER_WEBHOOK_HOST="127.0.0.1",
            MESSENGER_WEBHOOK_PORT=8181,
        ),
    )


@pytest.mark.asyncio
async def test_runtime_stop_handles_gateway_worker_and_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stops: list[str] = []

    async def stop_worker() -> None:
        stops.append("worker")

    gateway = FakeGatewayRuntime(
        dispatcher=SimpleNamespace(),
        config=SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(messenger_webhooks, "stop_delivery_worker", stop_worker)
    runner = FakeRunner(FakeApplication())
    runtime = messenger_webhooks.MessengerWebhookRuntime(
        runner=runner,
        site=FakeSite(runner, host="127.0.0.1", port=1),
        delivery_worker_started=True,
        bot_gateway_runtime=gateway,
    )
    await runtime.stop()
    assert gateway.stop_calls == 1
    assert runtime.bot_gateway_runtime is None
    assert stops == ["worker"]
    assert runtime.delivery_worker_started is False
    assert runner.cleanup_calls == 1
    await runtime.stop()
    assert gateway.stop_calls == 1
    assert stops == ["worker"]
    assert runner.cleanup_calls == 2


@pytest.mark.asyncio
async def test_runtime_stop_raises_first_error_after_all_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGatewayRuntime(
        dispatcher=SimpleNamespace(),
        config=SimpleNamespace(enabled=True),
    )
    gateway.stop_error = RuntimeError("gateway-stop")

    async def stop_worker() -> None:
        raise RuntimeError("worker-stop")

    monkeypatch.setattr(messenger_webhooks, "stop_delivery_worker", stop_worker)
    runner = FakeRunner(FakeApplication())
    runner.cleanup_fail = RuntimeError("runner-cleanup")
    runtime = messenger_webhooks.MessengerWebhookRuntime(
        runner=runner,
        site=FakeSite(runner, host="127.0.0.1", port=1),
        delivery_worker_started=True,
        bot_gateway_runtime=gateway,
    )
    with pytest.raises(RuntimeError, match="gateway-stop"):
        await runtime.stop()
    assert gateway.stop_calls == 1
    assert runner.cleanup_calls == 1
    assert runtime.bot_gateway_runtime is None
    assert runtime.delivery_worker_started is False


@pytest.mark.asyncio
async def test_health_and_environment_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    response = await messenger_webhooks._health(SimpleNamespace(app={}))
    assert response.status == 200
    assert json.loads(response.body) == {"ok": True, "service": "http-ingress"}

    gateway = SimpleNamespace(
        health_snapshot=lambda: {
            "running": True,
            "transport": "polling",
            "active_pollers": 2,
        }
    )
    monkeypatch.setattr(
        messenger_webhooks,
        "ManagedBotGatewayRuntime",
        type(gateway),
    )
    response = await messenger_webhooks._health(
        SimpleNamespace(app={"clientplatform_bot_gateway_runtime": gateway})
    )
    assert json.loads(response.body)["managed_bot_gateway"]["transport"] == "polling"

    monkeypatch.delenv("FLAG", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setattr(
        messenger_webhooks,
        "settings",
        SimpleNamespace(APP_ENV="dev"),
    )
    assert messenger_webhooks._truthy_env("FLAG") is False
    assert messenger_webhooks._deployed_env() is False
    monkeypatch.setenv("FLAG", " YES ")
    monkeypatch.setenv("APP_ENV", "staging")
    assert messenger_webhooks._truthy_env("FLAG") is True
    assert messenger_webhooks._deployed_env() is True


@pytest.mark.asyncio
async def test_max_official_secret_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, str]] = []

    async def handler(request: FakeRequest) -> Any:
        seen.append(dict(request.headers))
        return "ok"

    monkeypatch.setattr(messenger_webhooks, "max_webhook", handler)
    request = FakeRequest(headers={"X-Max-Bot-Api-Secret": " official "})
    assert await messenger_webhooks._max_webhook_with_official_secret(request) == "ok"
    assert seen[-1]["X-Max-Webhook-Secret"] == "official"
    assert request.cloned_headers is not None

    legacy = FakeRequest(
        headers={
            "X-Max-Bot-Api-Secret": "official",
            "X-Max-Webhook-Secret": "legacy",
        }
    )
    assert await messenger_webhooks._max_webhook_with_official_secret(legacy) == "ok"
    assert seen[-1]["X-Max-Webhook-Secret"] == "legacy"
    assert legacy.cloned_headers is None


@pytest.mark.parametrize(
    ("expected", "payload", "deployed", "allow", "result"),
    [
        ("", {}, False, True, True),
        ("", {}, False, False, False),
        ("", {}, True, True, False),
        ("10", {"group_id": 10}, True, False, True),
        ("10", {"group_id": "11"}, True, False, False),
        ("bad", {"group_id": 10}, True, False, False),
        ("10", {"group_id": "bad"}, True, False, False),
        ("0", {"group_id": 0}, False, False, False),
    ],
)
def test_vk_group_guard_matrix(
    monkeypatch: pytest.MonkeyPatch,
    expected: str,
    payload: dict[str, Any],
    deployed: bool,
    allow: bool,
    result: bool,
) -> None:
    monkeypatch.setattr(
        messenger_webhooks,
        "settings",
        SimpleNamespace(VK_GROUP_ID=expected),
    )
    monkeypatch.setattr(messenger_webhooks, "_deployed_env", lambda: deployed)
    monkeypatch.setattr(messenger_webhooks, "_truthy_env", lambda _name: allow)
    assert messenger_webhooks._vk_group_ok(payload) is result


@pytest.mark.asyncio
async def test_vk_webhook_guard_delegation_and_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegated: list[str] = []

    async def handler(request: FakeRequest) -> Any:
        delegated.append(await request.text())
        return "delegated"

    monkeypatch.setattr(messenger_webhooks, "vk_webhook", handler)
    monkeypatch.setattr(messenger_webhooks, "_vk_group_ok", lambda _payload: True)
    assert (
        await messenger_webhooks._vk_webhook_with_group_guard(FakeRequest("not-json"))
        == "delegated"
    )
    valid = json.dumps({"group_id": 10})
    assert (
        await messenger_webhooks._vk_webhook_with_group_guard(FakeRequest(valid))
        == "delegated"
    )
    monkeypatch.setattr(messenger_webhooks, "_vk_group_ok", lambda _payload: False)
    response = await messenger_webhooks._vk_webhook_with_group_guard(FakeRequest(valid))
    assert response.status == 403
    assert response.text == "forbidden"
    assert delegated == ["not-json", valid]


def test_route_registration_helpers_have_no_telegram_route() -> None:
    app = FakeApplication()
    messenger_webhooks._register_health_routes(app)
    messenger_webhooks._register_payment_routes(app)
    messenger_webhooks._register_privacy_export_routes(app)
    messenger_webhooks._register_max_routes(app)
    messenger_webhooks._register_vk_routes(app)
    messenger_webhooks._register_audio_routes(app)
    messenger_webhooks._register_clientplatform_owner_entry_routes(app)
    routes = {(method, path) for method, path, _handler in app.router.routes}
    assert ("GET", "/") in routes
    assert ("GET", "/health") in routes
    assert ("GET", "/healthz") in routes
    assert ("GET", "/terms") in routes
    assert ("POST", "/pay/yookassa/webhook") in routes
    assert ("POST", "/webhooks/max") in routes
    assert ("POST", "/webhooks/vk") in routes
    assert ("GET", "/clientplatform/open/{platform}") in routes
    assert all("telegram" not in path for _method, path in routes)
    assert any(path.endswith("{filename}") for _method, path in routes)
    assert any(path.endswith("{token}") for _method, path in routes)


def test_resolve_ingress_bind_uses_only_messenger_http_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        messenger_webhooks,
        "settings",
        SimpleNamespace(
            MESSENGER_WEBHOOK_HOST="127.0.0.1",
            MESSENGER_WEBHOOK_PORT=8181,
            TELEGRAM_WEBHOOK_HOST="0.0.0.0",
            TELEGRAM_WEBHOOK_PORT=9999,
        ),
    )
    assert messenger_webhooks._resolve_ingress_bind() == ("127.0.0.1", 8181)


@pytest.mark.asyncio
async def test_start_runtime_returns_none_when_every_ingress_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_surface(monkeypatch, ingress=False, gateway=False)
    assert await messenger_webhooks.start_messenger_webhook_runtime() is None
    assert FakeApplication.instances == []


@pytest.mark.asyncio
async def test_owner_entry_route_is_registered_without_omnichannel_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_surface(monkeypatch, ingress=True, gateway=False)
    monkeypatch.setattr(messenger_webhooks, "_omnichannel_ingress_enabled", lambda: False)
    monkeypatch.setattr(messenger_webhooks, "_acquisition_ingress_enabled", lambda: False)
    monkeypatch.setattr(messenger_webhooks, "external_product_ingress_enabled", lambda: False)
    monkeypatch.setattr(messenger_webhooks, "ad_oauth_http_enabled", lambda: False)
    monkeypatch.setattr(messenger_webhooks, "_ad_publication_worker_enabled", lambda: False)

    runtime = await messenger_webhooks.start_messenger_webhook_runtime()

    assert runtime is not None
    routes = {
        (method, path)
        for method, path, _handler in FakeApplication.instances[-1].router.routes
    }
    assert ("GET", "/clientplatform/open/{platform}") in routes
    assert ("POST", "/clientplatform/webhooks/vk/{route_id}") not in routes
    assert ("POST", "/clientplatform/webhooks/max/{route_id}") not in routes
    await runtime.stop()


@pytest.mark.asyncio
async def test_start_runtime_requires_dispatcher_for_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_surface(monkeypatch, gateway=True)
    with pytest.raises(RuntimeError, match="requires dispatcher"):
        await messenger_webhooks.start_messenger_webhook_runtime(
            bot=None,
            dispatcher=None,
        )
    assert len(FakeApplication.instances) == 1
    assert FakeRunner.instances == []


@pytest.mark.asyncio
async def test_full_webhook_native_ingress_and_polling_gateway_start_and_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_surface(
        monkeypatch,
        payment=True,
        privacy=True,
        max_enabled=True,
        vk_enabled=True,
        gateway=True,
    )
    worker_starts: list[str] = []
    worker_stops: list[str] = []
    monkeypatch.setattr(
        messenger_webhooks,
        "start_delivery_worker",
        lambda: worker_starts.append("start"),
    )

    async def stop_worker() -> None:
        worker_stops.append("stop")

    monkeypatch.setattr(messenger_webhooks, "stop_delivery_worker", stop_worker)
    dispatcher = SimpleNamespace(workflow_data={"task_manager": object()})
    ignored_bot = object()
    runtime = await messenger_webhooks.start_messenger_webhook_runtime(
        bot=ignored_bot,
        dispatcher=dispatcher,
    )
    assert runtime is not None
    assert runtime.telegram_public_url == ""
    assert runtime.delivery_worker_started is True
    assert runtime.bot_gateway_runtime is FakeGatewayRuntime.instances[-1]
    assert worker_starts == ["start"]
    assert FakeRunner.instances[-1].setup_calls == 1
    assert FakeSite.instances[-1].start_calls == 1
    assert FakeSite.instances[-1].host == "127.0.0.1"
    assert FakeSite.instances[-1].port == 8181
    routes = {
        (method, path)
        for method, path, _handler in FakeApplication.instances[-1].router.routes
    }
    assert ("POST", "/webhooks/max") in routes
    assert ("POST", "/webhooks/vk") in routes
    assert all("telegram" not in path for _method, path in routes)
    assert FakeGatewayRuntime.instances[-1].registered_apps == [
        FakeApplication.instances[-1]
    ]
    await runtime.stop()
    assert FakeGatewayRuntime.instances[-1].stop_calls == 1
    assert worker_stops == ["stop"]
    assert FakeRunner.instances[-1].cleanup_calls == 1


@pytest.mark.asyncio
async def test_gateway_start_failure_rolls_back_worker_and_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_surface(monkeypatch, max_enabled=True, gateway=True)
    FakeGatewayRuntime.start_result = False
    worker_stops: list[str] = []
    monkeypatch.setattr(messenger_webhooks, "start_delivery_worker", lambda: None)

    async def stop_worker() -> None:
        worker_stops.append("stop")

    monkeypatch.setattr(messenger_webhooks, "stop_delivery_worker", stop_worker)
    dispatcher = SimpleNamespace(workflow_data={"task_manager": object()})
    with pytest.raises(RuntimeError, match="failed to start"):
        await messenger_webhooks.start_messenger_webhook_runtime(
            dispatcher=dispatcher,
        )
    assert FakeGatewayRuntime.instances[-1].start_calls == 1
    assert FakeGatewayRuntime.instances[-1].stop_calls == 0
    assert worker_stops == ["stop"]
    assert FakeRunner.instances[-1].cleanup_calls == 1


@pytest.mark.asyncio
async def test_site_start_failure_rolls_back_runner_without_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_surface(monkeypatch, max_enabled=True, gateway=True)
    FakeSite.fail = OSError("bind-failed")
    dispatcher = SimpleNamespace(workflow_data={"task_manager": object()})
    with pytest.raises(OSError, match="bind-failed"):
        await messenger_webhooks.start_messenger_webhook_runtime(
            dispatcher=dispatcher,
        )
    assert FakeGatewayRuntime.instances[-1].start_calls == 0
    assert FakeRunner.instances[-1].cleanup_calls == 1


@pytest.mark.asyncio
async def test_delivery_worker_start_failure_cleans_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime_surface(monkeypatch, max_enabled=True, gateway=True)

    def broken_start() -> None:
        raise RuntimeError("worker-start")

    monkeypatch.setattr(messenger_webhooks, "start_delivery_worker", broken_start)
    dispatcher = SimpleNamespace(workflow_data={"task_manager": object()})
    with pytest.raises(RuntimeError, match="worker-start"):
        await messenger_webhooks.start_messenger_webhook_runtime(
            dispatcher=dispatcher,
        )
    assert FakeGatewayRuntime.instances[-1].start_calls == 0
    assert FakeRunner.instances[-1].cleanup_calls == 1
