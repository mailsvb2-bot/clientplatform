from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime import health_server


def scheduler_snapshot(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "scheduler_loop_task_running": True,
        "scheduler_loop_started": True,
        "scheduler_loop_error_count": 0,
        "scheduler_loop_last_error": "",
        "scheduler_loop_last_error_age_sec": 0,
        "scheduler_loop_last_tick_age_sec": 1,
        "payment_retry_active": 0,
        "payment_retry_dead": 0,
    }
    base.update(overrides)
    return base


def test_scheduler_snapshot_success_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = scheduler_snapshot()
    monkeypatch.setattr(health_server, "scheduler_health_snapshot", lambda: expected)
    assert health_server._scheduler_snapshot() is expected

    monkeypatch.setattr(
        health_server,
        "scheduler_health_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("down")),
    )
    fallback = health_server._scheduler_snapshot()
    assert fallback["scheduler_loop_task_running"] is False
    assert fallback["precise_scheduler_queue_size"] == 0


def test_integer_env_and_scheduler_error_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VALUE", raising=False)
    assert health_server._int_env("VALUE", 7) == 7
    monkeypatch.setenv("VALUE", " 9 ")
    assert health_server._int_env("VALUE", 7) == 9
    monkeypatch.setenv("VALUE", "bad")
    assert health_server._int_env("VALUE", 7) == 7

    assert health_server._scheduler_recent_error(scheduler_snapshot()) is False
    assert health_server._scheduler_recent_error(
        scheduler_snapshot(scheduler_loop_error_count="bad", scheduler_loop_last_error="x")
    ) is False
    assert health_server._scheduler_recent_error(
        scheduler_snapshot(scheduler_loop_error_count=1, scheduler_loop_last_error="")
    ) is False

    monkeypatch.setenv("SCHEDULER_READY_MAX_LAST_ERROR_AGE_SEC", "0")
    assert health_server._scheduler_recent_error(
        scheduler_snapshot(scheduler_loop_error_count=1, scheduler_loop_last_error="RuntimeError")
    ) is True

    monkeypatch.setenv("SCHEDULER_READY_MAX_LAST_ERROR_AGE_SEC", "10")
    assert health_server._scheduler_recent_error(
        scheduler_snapshot(
            scheduler_loop_error_count=1,
            scheduler_loop_last_error="RuntimeError",
            scheduler_loop_last_error_age_sec=9,
        )
    ) is True
    assert health_server._scheduler_recent_error(
        scheduler_snapshot(
            scheduler_loop_error_count=1,
            scheduler_loop_last_error="RuntimeError",
            scheduler_loop_last_error_age_sec=11,
        )
    ) is False


def test_scheduler_stale_and_readiness_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    assert health_server._scheduler_stale(
        scheduler_snapshot(scheduler_loop_task_running=False)
    ) is False
    assert health_server._scheduler_stale(
        scheduler_snapshot(scheduler_loop_started=False)
    ) is False

    monkeypatch.setenv("SCHEDULER_READY_MAX_LAST_TICK_AGE_SEC", "5")
    assert health_server._scheduler_stale(
        scheduler_snapshot(scheduler_loop_last_tick_age_sec=6)
    ) is True
    assert health_server._scheduler_stale(
        scheduler_snapshot(scheduler_loop_last_tick_age_sec="bad")
    ) is True

    monkeypatch.setenv("SCHEDULER_READY_MAX_LAST_TICK_AGE_SEC", "0")
    assert health_server._scheduler_stale(
        scheduler_snapshot(scheduler_loop_last_tick_age_sec=999)
    ) is False

    monkeypatch.setenv("PAYMENT_RETRY_READY_MAX_ACTIVE", "2")
    monkeypatch.setenv("PAYMENT_RETRY_READY_MAX_DEAD", "0")
    ok, errors, flags = health_server._scheduler_readiness(scheduler_snapshot())
    assert ok is True
    assert errors == []
    assert flags["scheduler_degraded"] is False

    bad = scheduler_snapshot(
        scheduler_loop_task_running=False,
        scheduler_loop_error_count=1,
        scheduler_loop_last_error="RuntimeError",
        scheduler_loop_last_error_age_sec=0,
        payment_retry_active=3,
        payment_retry_dead=1,
    )
    ok, errors, flags = health_server._scheduler_readiness(bad)
    assert ok is False
    assert "scheduler:not_running" in errors
    assert "scheduler:recent_owner_tick_error" in errors
    assert "payment_retry:backlog" in errors
    assert "payment_retry:dead_letter" in errors
    assert flags["scheduler_degraded"] is True

    unavailable = scheduler_snapshot(payment_retry_active="bad", payment_retry_dead=None)
    ok, errors, flags = health_server._scheduler_readiness(unavailable)
    assert ok is False
    assert "payment_retry:unavailable" in errors
    assert flags["payment_retry_unavailable"] is True


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


def test_database_schema_audio_and_storage_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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

    monkeypatch.setattr(health_server, "audio_readiness", lambda: (False, "audio:missing"))
    assert health_server._audio_ready("dev") == (True, None)
    assert health_server._audio_ready("production") == (False, "audio:missing")

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
        SimpleNamespace(
            channel="payment",
            ok=True,
            missing=(),
            warnings=(),
            details={},
        ),
    ]
    monkeypatch.setattr(health_server, "check_all_preflights", lambda: statuses)
    ok, errors, details = health_server._messenger_preflight_readiness()
    assert ok is False
    assert errors == ["ingress:max:missing:MAX_TOKEN"]
    assert details["max_preflight_enabled"] is True
    assert details["vk_preflight_enabled"] is False
    assert details["payment_preflight_ok"] is True

    monkeypatch.setattr(health_server, "payment_http_enabled", lambda: True)
    monkeypatch.setattr(health_server, "max_webhook_enabled", lambda: False)
    monkeypatch.setattr(health_server, "vk_webhook_enabled", lambda: True)
    monkeypatch.setattr(health_server, "http_ingress_enabled", lambda: True)
    assert health_server._ingress_health_fields() == {
        "payment_http_enabled": True,
        "max_webhook_enabled": False,
        "vk_webhook_enabled": True,
        "http_ingress_enabled": True,
    }


def patch_common_payload_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_server, "_scheduler_snapshot", lambda: scheduler_snapshot(extra="value"))
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


def test_build_health_and_readiness_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_common_payload_dependencies(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "0")

    payload, status = health_server.build_health_payload()
    assert status == 200
    assert payload["ok"] is True
    assert payload["probe"] == "health"
    assert payload["db_target"] == "redacted"
    assert payload["extra"] == "value"

    monkeypatch.setattr(health_server, "_db_ready", lambda: (True, None))
    monkeypatch.setattr(health_server, "_schema_ready", lambda: (True, None))
    monkeypatch.setattr(health_server, "_scheduler_readiness", lambda _s: (True, [], {"scheduler_degraded": False}))
    monkeypatch.setattr(health_server, "_audio_ready", lambda _env: (True, None))
    monkeypatch.setattr(health_server, "required_readiness_tables", lambda: ("users", "jobs"))
    ready, status = health_server.build_readiness_payload()
    assert status == 200
    assert ready["ok"] is True
    assert ready["required_tables"] == ("users", "jobs")

    monkeypatch.setattr(health_server, "_db_ready", lambda: (False, "db:RuntimeError"))
    monkeypatch.setattr(health_server, "_schema_ready", lambda: (False, "schema:missing"))
    monkeypatch.setattr(
        health_server,
        "_scheduler_readiness",
        lambda _s: (False, ["scheduler:not_running"], {"scheduler_degraded": True}),
    )
    monkeypatch.setattr(
        health_server,
        "_messenger_preflight_readiness",
        lambda: (False, ["ingress:max:missing:token"], {"max_preflight_ok": False}),
    )
    monkeypatch.setattr(health_server, "_audio_ready", lambda _env: (False, "audio:missing"))
    monkeypatch.setattr(health_server, "http_ingress_enabled", lambda: True)
    monkeypatch.setattr(health_server, "_webhook_configured", lambda: False)
    failed, status = health_server.build_readiness_payload()
    assert status == 500
    assert failed["ok"] is False
    assert "db:RuntimeError" in failed["error"]
    assert "schema:missing" in failed["error"]
    assert "webhook:not_ready" in failed["error"]


@pytest.mark.asyncio
async def test_http_handlers_and_growth_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_server, "build_health_payload", lambda: ({"ok": True}, 201))
    monkeypatch.setattr(health_server, "build_readiness_payload", lambda: ({"ok": False}, 503))
    monkeypatch.setattr(
        health_server.web,
        "json_response",
        lambda payload, status: SimpleNamespace(payload=payload, status=status),
    )
    assert (await health_server._health(SimpleNamespace())).status == 201
    assert (await health_server._ready(SimpleNamespace())).status == 503

    calls: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(health_server, "build_click_redirect_target", lambda payload: f"https://example/{payload}")
    monkeypatch.setattr(
        health_server,
        "record_click_redirect",
        lambda payload, request_meta: calls.append((payload, request_meta)),
    )
    request = SimpleNamespace(
        match_info={"payload": "abc"},
        headers={"User-Agent": "ua", "Referer": "ref"},
    )
    response = await health_server._growth_click_redirect(request)
    assert response.location == "https://example/abc"
    assert calls == [("abc", {"user_agent": "ua", "referer": "ref"})]

    monkeypatch.setattr(
        health_server,
        "record_click_redirect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("skip")),
    )
    response = await health_server._growth_click_redirect(request)
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
