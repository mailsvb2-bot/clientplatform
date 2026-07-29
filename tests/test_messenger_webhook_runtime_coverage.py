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
    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.kwargs = kwargs
        self.router = FakeRouter()


class FakeRunner:
    instances: list["FakeRunner"] = []

    def __init__(self, app: Any) -> None:
        self.app = app
        self.setup_calls = 0
        self.cleanup_calls = 0
        self.instances.append(self)

    async def setup(self) -> None:
        self.setup_calls += 1

    async def cleanup(self) -> None:
        self.cleanup_calls += 1


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


@pytest.mark.asyncio
async def test_runtime_stop_handles_worker_and_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    stops: list[str] = []

    async def stop_worker() -> None:
        stops.append("worker")

    monkeypatch.setattr(messenger_webhooks, "stop_delivery_worker", stop_worker)
    runner = FakeRunner(FakeApplication())
    runtime = messenger_webhooks.MessengerWebhookRuntime(
        runner=runner,
        site=FakeSite(runner, host="127.0.0.1", port=1),
        delivery_worker_started=True,
    )
    await runtime.stop()
    assert stops == ["worker"]
    assert runtime.delivery_worker_started is False
    assert runner.cleanup_calls == 1
    await runtime.stop()
    assert stops == ["worker"]
    assert runner.cleanup_calls == 2


@pytest.mark.asyncio
async def test_health_and_environment_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    response = await messenger_webhooks._health(SimpleNamespace(app={}))
    assert response.status == 200
    assert json.loads(response.body) == {"ok": True, "service": "http-ingress"}

    gateway = SimpleNamespace(health_snapshot=lambda: {"running": True, "pending": 2})
    monkeypatch.setattr(
        messenger_webhooks,
        "ManagedBotGatewayRuntime",
        type(gateway),
    )
    response = await messenger_webhooks._health(
        SimpleNamespace(app={"clientplatform_bot_gateway_runtime": gateway})
    )
    assert json.loads(response.body)["managed_bot_gateway"] == {
        "running": True,
        "pending": 2,
    }

    monkeypatch.delenv("FLAG", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setattr(messenger_webhooks, "settings", SimpleNamespace(APP_ENV="dev"))
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
    monkeypatch.setattr(messenger_webhooks, "settings", SimpleNamespace(VK_GROUP_ID=expected))
    monkeypatch.setattr(messenger_webhooks, "_deployed_env", lambda: deployed)
    monkeypatch.setattr(messenger_webhooks, "_truthy_env", lambda _name: allow)
    assert messenger_webhooks._vk_group_ok(payload) is result


@pytest.mark.asyncio
async def test_vk_webhook_guard_delegation_and_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    delegated: list[str] = []

    async def handler(request: FakeRequest) -> Any:
        delegated.append(await request.text())
        return "delegated"

    monkeypatch.setattr(messenger_webhooks, "vk_webhook", handler)
    assert await messenger_webhooks._vk_webhook_with_group_guard(FakeRequest("not-json")) == "delegated"
