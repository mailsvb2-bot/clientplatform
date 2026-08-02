from __future__ import annotations

import socket
from typing import Any

import pytest

from core.telegram_bot import ResilientBot, telegram_connector_options


def _policy_bot() -> ResilientBot:
    bot = object.__new__(ResilientBot)
    bot._ui_request_timeout = 3.0
    bot._callback_request_timeout = 1.0
    bot._polling_request_timeout = 55.0
    bot._ui_network_retries = 1
    bot._polling_network_retries = 2
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


@pytest.mark.parametrize(
    ("method_name", "request_timeout", "expected"),
    [
        ("getUpdates", None, (55.0, 2, "polling")),
        ("answerCallbackQuery", None, (1.0, 0, "callback")),
        ("editMessageText", None, (3.0, 1, "ui")),
        ("sendMessage", None, (3.0, 1, "ui")),
        ("getMe", None, (3.0, 1, "ui")),
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
