from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import pytest

from runtime import health_server


async def direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


def response_factory(payload: dict[str, Any], status: int) -> SimpleNamespace:
    return SimpleNamespace(payload=payload, status=status)


def request(headers: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(headers=headers or {})


@pytest.mark.asyncio
async def test_public_health_probe_omits_internal_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HEALTHCHECK_DIAGNOSTICS_TOKEN", raising=False)
    monkeypatch.setattr(health_server.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(health_server.web, "json_response", response_factory)
    monkeypatch.setattr(
        health_server,
        "build_health_payload",
        lambda: (
            {
                "ok": True,
                "service": "clientplatform",
                "probe": "health",
                "db_target": "postgresql://secret-host/clientplatform",
                "legacy_sqlite_path": "/srv/private/data.sqlite",
                "scheduler_loop_last_error": "RuntimeError: secret detail",
                "max_preflight_missing": ["MAX_TOKEN"],
                "ai_policy": "internal",
            },
            200,
        ),
    )

    response = await health_server._health(request())

    assert response.status == 200
    assert response.payload == {
        "ok": True,
        "service": "clientplatform",
        "probe": "health",
    }


@pytest.mark.asyncio
async def test_public_readiness_preserves_failure_status_without_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEALTHCHECK_DIAGNOSTICS_TOKEN", "operator-secret")
    monkeypatch.setattr(health_server.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(health_server.web, "json_response", response_factory)
    monkeypatch.setattr(
        health_server,
        "build_readiness_payload",
        lambda: (
            {
                "ok": False,
                "service": "clientplatform",
                "probe": "readiness",
                "error": "db:password leaked;ingress:max:missing:MAX_TOKEN",
                "db_target": "postgresql://private-host/clientplatform",
                "required_tables": ("users", "payments"),
            },
            500,
        ),
    )

    response = await health_server._ready(request({"X-ClientPlatform-Diagnostics-Token": "wrong"}))

    assert response.status == 500
    assert response.payload == {
        "ok": False,
        "service": "clientplatform",
        "probe": "readiness",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"X-ClientPlatform-Diagnostics-Token": "operator-secret"},
        {"Authorization": "Bearer operator-secret"},
        {"Authorization": "bearer operator-secret"},
    ],
)
async def test_authorized_operator_receives_full_health_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> None:
    detailed = {
        "ok": True,
        "service": "clientplatform",
        "probe": "health",
        "db_engine": "postgres",
        "db_target": "redacted",
        "scheduler_loop_task_running": True,
    }
    monkeypatch.setenv("HEALTHCHECK_DIAGNOSTICS_TOKEN", "operator-secret")
    monkeypatch.setattr(health_server.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(health_server.web, "json_response", response_factory)
    monkeypatch.setattr(health_server, "build_health_payload", lambda: (detailed, 200))

    response = await health_server._health(request(headers))

    assert response.status == 200
    assert response.payload == detailed


def test_diagnostics_authorization_fails_closed_without_server_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HEALTHCHECK_DIAGNOSTICS_TOKEN", raising=False)

    assert health_server._diagnostics_authorized(
        request({"X-ClientPlatform-Diagnostics-Token": "anything"})
    ) is False
    assert health_server._diagnostics_authorized(
        request({"Authorization": "Bearer anything"})
    ) is False
    assert health_server._provided_diagnostics_token(request({"Authorization": "Basic abc"})) == ""
