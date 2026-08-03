from __future__ import annotations

import asyncio

import pytest

from core import telegram_multi_egress as multi


@pytest.mark.asyncio
async def test_ui_and_polling_can_use_physically_independent_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_UI_PROXY_URL", "http://127.0.0.1:18080")
    monkeypatch.setenv("TELEGRAM_POLLING_PROXY_URL", "http://127.0.0.1:18081")
    monkeypatch.setenv(
        "CLIENTPLATFORM_TELEGRAM_API_IPV4_POOL",
        "149.154.167.220",
    )

    bot = multi.MultiEgressResilientBot(token="123456:TEST_TOKEN_VALUE")
    try:
        assert bot.session is not bot.polling_session
        assert bot.session.transport_role == "ui"
        assert bot.polling_session.transport_role == "polling"
        snapshot = multi.telegram_egress_snapshot()
        assert snapshot.ui_mode == "proxy"
        assert snapshot.polling_mode == "proxy"
        assert snapshot.egress_redundant is True
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_single_direct_route_is_isolated_but_not_falsely_redundant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_UI_PROXY_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_POLLING_PROXY_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_PROXY_URL", raising=False)
    monkeypatch.setenv(
        "CLIENTPLATFORM_TELEGRAM_API_IPV4_POOL",
        "149.154.167.220",
    )

    bot = multi.MultiEgressResilientBot(token="123456:TEST_TOKEN_VALUE")
    try:
        assert bot.session is not bot.polling_session
        assert bot.session.active_route == "149.154.167.220"
        assert bot.polling_session.active_route == "149.154.167.220"
        assert multi.telegram_egress_snapshot().egress_redundant is False
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_polling_reset_does_not_mutate_ui_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CLIENTPLATFORM_TELEGRAM_API_IPV4_POOL",
        "149.154.167.220,149.154.167.221",
    )
    bot = multi.MultiEgressResilientBot(token="123456:TEST_TOKEN_VALUE")
    try:
        ui_route = bot.session.active_route
        ui_generation = bot.session.transport_generation
        polling_route = bot.polling_session.active_route

        assert await bot.polling_session.reset_transport(
            observed_generation=bot.polling_session.transport_generation,
        ) is True
        assert bot.session.active_route == ui_route
        assert bot.session.transport_generation == ui_generation
        assert bot.polling_session.active_route != polling_route
    finally:
        await bot.session.close()


def test_polling_is_ready_while_long_poll_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CLIENTPLATFORM_TELEGRAM_POLLING_INFLIGHT_MAX_AGE_SEC",
        "70",
    )
    state = multi._TransportState()
    state.configure(
        ui_mode="direct",
        polling_mode="direct",
        ui_route="149.154.167.220",
        polling_route="149.154.167.220",
        route_pool_size=1,
        egress_redundant=False,
    )
    state.begin("polling", "149.154.167.220")

    snapshot = state.snapshot()
    assert snapshot.polling_in_flight is True
    assert snapshot.polling_ready is True

    state.failure("polling", "149.154.167.220")
    failed = state.snapshot()
    assert failed.polling_in_flight is False
    assert failed.polling_ready is False
    assert failed.polling_failures == 1


def test_direct_and_proxy_lanes_are_independent_even_with_one_telegram_ip() -> None:
    assert multi._redundant(
        ui_proxy=None,
        polling_proxy="http://127.0.0.1:18081",
        ui_routes=("149.154.167.220",),
        polling_routes=("149.154.167.220",),
        ui_route="149.154.167.220",
        polling_route="149.154.167.220",
    ) is True


def test_redundancy_requirement_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(
        "CLIENTPLATFORM_REQUIRE_REDUNDANT_TELEGRAM_EGRESS",
        raising=False,
    )
    assert multi.telegram_redundancy_required() is False
    monkeypatch.setenv(
        "CLIENTPLATFORM_REQUIRE_REDUNDANT_TELEGRAM_EGRESS",
        "true",
    )
    assert multi.telegram_redundancy_required() is True
