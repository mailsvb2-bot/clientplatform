from __future__ import annotations

from core.telegram_bot import ResilientBot


def _policy_bot() -> ResilientBot:
    bot = object.__new__(ResilientBot)
    bot._polling_request_timeout = 55.0
    bot._polling_network_retries = 2
    bot._callback_request_timeout = 0.75
    bot._callback_network_retries = 1
    bot._ui_request_timeout = 2.0
    bot._ui_network_retries = 1
    return bot


def test_aiogram_derived_polling_timeout_cannot_shrink_configured_budget() -> None:
    bot = _policy_bot()

    timeout, retries, policy = bot.request_policy("getUpdates", 12)

    assert timeout == 55.0
    assert retries == 2
    assert policy == "polling"


def test_polling_policy_preserves_intentionally_longer_caller_timeout() -> None:
    bot = _policy_bot()

    timeout, retries, policy = bot.request_policy("GETUPDATES", 70)

    assert timeout == 70.0
    assert retries == 2
    assert policy == "polling"


def test_polling_policy_uses_configured_budget_without_caller_timeout() -> None:
    bot = _policy_bot()

    timeout, retries, policy = bot.request_policy("getupdates")

    assert timeout == 55.0
    assert retries == 2
    assert policy == "polling"


def test_polling_policy_preserves_non_numeric_explicit_timeout_contract() -> None:
    bot = _policy_bot()
    sentinel = object()

    timeout, retries, policy = bot.request_policy("getupdates", sentinel)

    assert timeout is sentinel
    assert retries == 2
    assert policy == "polling"


def test_polling_floor_does_not_change_callback_or_ui_timeout_policy() -> None:
    bot = _policy_bot()

    callback_timeout, callback_retries, callback_policy = bot.request_policy(
        "answerCallbackQuery",
        0.5,
    )
    ui_timeout, ui_retries, ui_policy = bot.request_policy("sendMessage", 4.0)

    assert callback_timeout == 0.5
    assert callback_retries == 1
    assert callback_policy == "callback"
    assert ui_timeout == 4.0
    assert ui_retries == 1
    assert ui_policy == "ui"
