from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime import health_server


def test_transport_and_webhook_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_server.settings, "MESSENGER_WEBHOOK_ENABLED", True, raising=False)
    assert health_server._messenger_webhook_configured() is True
    monkeypatch.delattr(health_server.settings, "MESSENGER_WEBHOOK_ENABLED", raising=False)
    assert health_server._messenger_webhook_configured() is False

    monkeypatch.setattr(health_server, "telegram_transport", lambda: "webhook")
    assert health_server._telegram_transport() == "webhook"
    assert health_server._telegram_webhook_configured() is True

    monkeypatch.setattr(
        health_server,
        "telegram_transport",
        lambda: (_ for _ in ()).throw(RuntimeError("bad")),
    )
    assert health_server._telegram_transport() == "unknown"

    monkeypatch.setattr(health_server, "http_ingress_enabled", lambda: False)
    monkeypatch.setattr(health_server, "_telegram_webhook_configured", lambda: False)
    assert health_server._webhook_configured() is False
    monkeypatch.setattr(health_server, "http_ingress_enabled", lambda: True)
    assert health_server._webhook_configured() is True


def test_database_schema_and_storage_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Conn:
        def __enter__(self) -> "Conn":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def execute(self, _query: str) -> Any:
            return SimpleNamespace(fetchone=lambda: (1,))

    monkeypatch.setattr(health_server, "get_connection", lambda: Conn())
    assert health_server._db_ready() == (True, None)

    monkeypatch.setattr(
        health_server,
        "get_connection",
        lambda: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    ok, error = health_server._db_ready()
    assert ok is False
    assert error and error.startswith("db:")

    monkeypatch.setattr(health_server, "schema_readiness", lambda: (True, None))
    assert health_server._schema_ready() == (True, None)

    root = tmp_path / "root"
    root.mkdir()
    db_path = tmp_path / "db.sqlite"
    db_path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(health_server, "ROOT", root)
    monkeypatch.setattr(health_server, "DB_PATH", db_path)

    monkeypatch.setattr(health_server, "CONFIG", SimpleNamespace(uses_postgres=False, engine="sqlite"))
    fields = health_server._storage_health_fields()
    assert fields["root_exists"] is True
    assert fields["db_exists"] is True

    monkeypatch.setattr(health_server, "CONFIG", SimpleNamespace(uses_postgres=True, engine="postgres"))
    fields = health_server._storage_health_fields()
    assert fields["legacy_sqlite_present"] is True

    class BadPath:
        def exists(self) -> bool:
            raise OSError("disk")

        def __str__(self) -> str:
            return "bad"

    monkeypatch.setattr(health_server, "ROOT", BadPath())
    monkeypatch.setattr(health_server, "DB_PATH", BadPath())
    fields = health_server._storage_health_fields()
    assert fields["root_exists"] is False
    assert fields["legacy_sqlite_present"] is False


def test_messenger_preflight_and_ingress_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    statuses = [
        SimpleNamespace(
            channel="max",
            ok=False,
            missing=("MAX_TOKEN",),
            warnings=("warn",),
            details={"enabled": True, "mode": "webhook"},
        ),
        SimpleNamespace(
            channel="vk",
            ok=False,
            missing=("VK_TOKEN",),
            warnings=(),
            details={"enabled": False},
        ),
    ]
    monkeypatch.setattr(health_server, "check_all_preflights", lambda: statuses)
    ok, errors, details = health_server._messenger_preflight_readiness()
    assert ok is False
    assert errors == ["ingress:max:missing:MAX_TOKEN"]
    assert details["max_preflight_enabled"] is True
    assert details["vk_preflight_enabled"] is False

    monkeypatch.setattr(health_server, "max_webhook_enabled", lambda: False)
    monkeypatch.setattr(health_server, "vk_webhook_enabled", lambda: True)
    monkeypatch.setattr(health_server, "http_ingress_enabled", lambda: True)
    assert health_server._ingress_health_fields() == {
        "max_webhook_enabled": False,
        "vk_webhook_enabled": True,
        "http_ingress_enabled": True,
    }


def patch_common_payload_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_server, "_telegram_transport", lambda: "polling")
    monkeypatch.setattr(health_server, "_messenger_webhook_configured", lambda: False)
    monkeypatch.setattr(health_server, "_webhook_configured", lambda: False)
    monkeypatch.setattr(health_server, "_ingress_health_fields", lambda: {"http_ingress_enabled": False})
    monkeypatch.setattr(health_server, "_storage_health_fields", lambda: {"root_exists": True})
    monkeypatch.setattr(health_server, "ai_policy_snapshot", lambda: {"ai_policy": "ok"})
    monkeypatch.setattr(
        health_server,
        "_messenger_preflight_readiness",
        lambda: (True, [], {"max_preflight_ok": True}),
    )
    monkeypatch.setattr(health_server, "redacted_db_target", lambda: "redacted")
    monkeypatch.setattr(health_server, "CONFIG", SimpleNamespace(engine="postgres", uses_postgres=True))
    monkeypatch.setattr(health_server, "clientplatform_runtime_snapshot", lambda: {"dispatch_runtime_enabled": True})
    monkeypatch.setattr(
        health_server,
        "clientplatform_dispatch_readiness",
        lambda _snapshot: (True, [], {"clientplatform_dispatch_degraded": False}),
    )


def test_build_health_and_readiness_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_common_payload_dependencies(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")

    payload, status = health_server.build_health_payload()
    assert status == 200
    assert payload["ok"] is True
    assert payload["probe"] == "health"
    assert payload["db_target"] == "redacted"

    monkeypatch.setattr(health_server, "_db_ready", lambda: (True, None))
    monkeypatch.setattr(health_server, "_schema_ready", lambda: (True, None))
    monkeypatch.setattr(health_server, "required_readiness_tables", lambda: ("users", "jobs"))
    ready, status = health_server.build_readiness_payload()
    assert status == 200
    assert ready["ok"] is True
    assert ready["required_tables"] == ("users", "jobs")

    monkeypatch.setattr(health_server, "_db_ready", lambda: (False, "db:RuntimeError"))
    monkeypatch.setattr(health_server, "_schema_ready", lambda: (False, "schema:missing"))
    monkeypatch.setattr(
        health_server,
        "_messenger_preflight_readiness",
        lambda: (False, ["ingress:max:missing:token"], {"max_preflight_ok": False}),
    )
    monkeypatch.setattr(health_server, "http_ingress_enabled", lambda: True)
    monkeypatch.setattr(health_server, "_webhook_configured", lambda: False)
    failed, status = health_server.build_readiness_payload()
    assert status == 500
    assert failed["ok"] is False
    assert "db:RuntimeError" in failed["error"]
    assert "schema:missing" in failed["error"]
    assert "webhook:not_ready" in failed["error"]


@pytest.mark.asyncio
async def test_http_handlers_and_historical_redirect_are_side_effect_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_server, "build_health_payload", lambda: ({"ok": True}, 201))
    monkeypatch.setattr(health_server, "build_readiness_payload", lambda: ({"ok": False}, 503))
    monkeypatch.setattr(
        health_server.web,
        "json_response",
        lambda payload, status: SimpleNamespace(payload=payload, status=status),
    )
    request = SimpleNamespace(headers={}, match_info={})
    assert (await health_server._health(request)).status == 201
    assert (await health_server._ready(request)).status == 503

    monkeypatch.setattr(health_server, "historical_start_redirect", lambda payload: f"https://example/{payload}")
    response = await health_server._historical_start_redirect(
        SimpleNamespace(match_info={"payload": "abc"})
    )
    assert response.location == "https://example/abc"



class FakeRouter:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Any]] = []

    def add_get(self, path: str, handler: Any) -> None:
        self.routes.append(("GET", path, handler))


class FakeApplication:
    def __init__(self) -> None:
        self.router = FakeRouter()


class FakeRunner:
    def __init__(self, app: FakeApplication) -> None:
        self.app = app
        self.setup_called = False
        self.cleanup_called = False

    async def setup(self) -> None:
        self.setup_called = True

    async def cleanup(self) -> None:
        self.cleanup_called = True


@pytest.mark.asyncio
async def test_health_runtime_disabled_success_stop_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_server.settings, "HEALTHCHECK_ENABLED", False, raising=False)
    assert await health_server.start_health_runtime() is None

    monkeypatch.setattr(health_server.settings, "HEALTHCHECK_ENABLED", True, raising=False)
    monkeypatch.setattr(health_server.settings, "HEALTHCHECK_HOST", "127.0.0.1", raising=False)
    monkeypatch.setattr(health_server.settings, "HEALTHCHECK_PORT", 8082, raising=False)
    monkeypatch.setattr(health_server.web, "Application", FakeApplication)
    monkeypatch.setattr(health_server.web, "AppRunner", FakeRunner)

    sites: list[Any] = []

    class Site:
        def __init__(self, runner: FakeRunner, host: str, port: int) -> None:
            self.runner = runner
            self.host = host
            self.port = port
            self.started = False
            sites.append(self)

        async def start(self) -> None:
            self.started = True

    monkeypatch.setattr(health_server.web, "TCPSite", Site)
    runtime = await health_server.start_health_runtime()
    assert runtime is not None
    assert runtime.runner.setup_called is True
    assert sites[0].started is True
    assert {path for _, path, _ in runtime.runner.app.router.routes} == {
        "/a/{payload}", "/health", "/healthz", "/readyz"
    }
    await runtime.stop()
    assert runtime.runner.cleanup_called is True

    class FailingSite(Site):
        async def start(self) -> None:
            raise OSError("bind")

    monkeypatch.setattr(health_server.web, "TCPSite", FailingSite)
    with pytest.raises(OSError, match="bind"):
        await health_server.start_health_runtime()
    assert sites[-1].runner.cleanup_called is True
