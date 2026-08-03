from __future__ import annotations

import socket
from collections import deque
from typing import Any

import pytest

from core.telegram_bot import (
    ResilientBot,
    TelegramRouteResolver,
    telegram_connector_options,
    telegram_route_pool,
)


def _policy_bot() -> ResilientBot:
    bot = object.__new__(ResilientBot)
    bot._ui_request_timeout = 2.0
    bot._callback_request_timeout = 0.75
    bot._polling_request_timeout = 55.0
    bot._ui_network_retries = 1
    bot._callback_network_retries = 1
    bot._polling_network_retries = 2
    bot._latency_samples = deque(maxlen=256)
    return bot


def test_default_connector_reuses_short_lived_telegram_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "TELEGRAM_FORCE_CLOSE",
        "TELEGRAM_KEEPALIVE_TIMEOUT_SEC",
        "TELEGRAM_CONNECTIONS_PER_HOST",
        "TELEGRAM_IP_FAMILY",
    ):
        monkeypatch.delenv(name, raising=False)

    options = telegram_connector_options()

    assert options["family"] == socket.AF_INET
    assert options["force_close"] is False
    assert options["keepalive_timeout"] == 20.0
    assert options["limit_per_host"] == 20
    assert options["enable_cleanup_closed"] is True


def test_force_close_mode_remains_available_without_invalid_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_FORCE_CLOSE", "1")

    options = telegram_connector_options()

    assert options["force_close"] is True
    assert "keepalive_timeout" not in options


def test_route_pool_is_validated_ordered_and_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CLIENTPLATFORM_TELEGRAM_API_IPV4_POOL",
        "149.154.167.220, invalid,149.154.167.221;149.154.167.220",
    )
    monkeypatch.setenv(
        "CLIENTPLATFORM_TELEGRAM_API_IPV4",
        "149.154.167.222",
    )

    assert telegram_route_pool() == (
        "149.154.167.220",
        "149.154.167.221",
        "149.154.167.222",
    )


@pytest.mark.asyncio
async def test_route_resolver_rotates_only_telegram_api_host() -> None:
    resolver = TelegramRouteResolver(
        ("149.154.167.220", "149.154.167.221"),
    )

    first = await resolver.resolve("api.telegram.org", 443)
    assert first[0]["host"] == "149.154.167.220"
    assert resolver.rotate() is True
    second = await resolver.resolve("api.telegram.org", 443)
    assert second[0]["host"] == "149.154.167.221"

    await resolver.close()


@pytest.mark.parametrize(
    ("method_name", "request_timeout", "expected"),
    [
        ("getUpdates", None, (55.0, 2, "polling")),
        ("answerCallbackQuery", None, (0.75, 1, "callback")),
        ("editMessageText", None, (2.0, 1, "ui")),
        ("sendMessage", None, (2.0, 1, "ui")),
        ("getMe", None, (2.0, 1, "ui")),
        ("getUpdates", 61.0, (61.0, 2, "polling")),
        ("editMessageText", 7.0, (7.0, 1, "ui")),
    ],
)
def test_method_specific_request_policy_keeps_ui_fast_and_polling_long(
    method_name: str,
    request_timeout: Any,
    expected: tuple[Any, int, str],
) -> None:
    assert _policy_bot().request_policy(method_name, request_timeout) == expected


def test_latency_snapshot_reports_p50_p95_max_and_failures() -> None:
    bot = _policy_bot()
    for index in range(1, 31):
        bot._latency_samples.append(
            {
                "method": "editMessageText",
                "policy": "ui",
                "elapsed_ms": float(index * 10),
                "success": index != 30,
                "route": "149.154.167.220",
                "generation": 0,
            }
        )

    assert bot.latency_snapshot(policy="ui") == {
        "count": 30,
        "successes": 29,
        "failures": 1,
        "p50_ms": 150.0,
        "p95_ms": 290.0,
        "max_ms": 300.0,
    }
