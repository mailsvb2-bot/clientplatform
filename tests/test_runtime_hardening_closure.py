from __future__ import annotations

from types import SimpleNamespace

import pytest

import app
from services.db import core as db_core


def test_runtime_numeric_parsers_fail_closed(monkeypatch):
    monkeypatch.setenv("SLOW_HANDLER_MS", "broken")
    monkeypatch.setenv("SOFT_CALLBACK_RATE_LIMIT_SEC", "-5")
    assert app._runtime_int("SLOW_HANDLER_MS", 700, minimum=1) == 700
    assert app._runtime_float("SOFT_CALLBACK_RATE_LIMIT_SEC", 0.05, minimum=0.0) == 0.05


@pytest.mark.asyncio
async def test_partial_startup_rollback_runs_in_reverse_order(monkeypatch):
    calls: list[str] = []

    class Runtime:
        def __init__(self, name: str):
            self.name = name

        async def stop(self):
            calls.append(self.name)

    async def stop_db_writer(*, drain: bool):
        assert drain is False
        calls.append("db_writer")

    monkeypatch.setattr(app, "stop_db_writer", stop_db_writer)

    await app._rollback_partial_startup(
        webhook_runtime=Runtime("webhook"),
        health_runtime=Runtime("health"),
        db_writer_started=True,
    )

    assert calls == ["health", "webhook", "db_writer"]


def test_cached_postgres_connection_is_pre_pinged_and_rolled_back():
    calls: list[str] = []

    class FakeConnection:
        closed = False

        def execute(self, sql: str):
            calls.append(sql)
            return SimpleNamespace()

        def rollback(self):
            calls.append("rollback")

    assert db_core._raw_pg_connection_is_usable(FakeConnection()) is True
    assert calls == ["SELECT 1", "rollback"]


def test_broken_cached_postgres_connection_is_rejected():
    class BrokenConnection:
        closed = False

        def execute(self, _sql: str):
            raise OSError("socket closed")

        def rollback(self):
            raise AssertionError("rollback must not be reached")

    assert db_core._raw_pg_connection_is_usable(BrokenConnection()) is False
