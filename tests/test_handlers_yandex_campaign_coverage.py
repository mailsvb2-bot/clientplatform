from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from clientplatform.application.yandex_campaign_diagnostics import (
    YandexCampaignDiagnosticsRow,
    YandexCampaignDiagnosticsSnapshot,
)
from clientplatform.integrations.yandex_direct import YandexDirectError


yandex = importlib.import_module("handlers.clientplatform_yandex_analytics")
control = importlib.import_module("handlers.clientplatform_control")


class _State:
    def __init__(self) -> None:
        self.cleared = 0

    async def clear(self) -> None:
        self.cleared += 1


class _Message:
    def __init__(self) -> None:
        self.edits: list[tuple[str, dict]] = []
        self.answers: list[tuple[str, dict]] = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append((text, kwargs))


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=101)
        self.message = _Message()
        self.answers: list[tuple[tuple, dict]] = []

    async def answer(self, *args, **kwargs) -> None:
        self.answers.append((args, kwargs))


def _snapshot(*, connected: int = 1, managed: int = 1, cost=None, provider_row=False):
    campaigns = ()
    if managed:
        campaigns = (
            YandexCampaignDiagnosticsRow(
                connection_id=str(uuid4()),
                campaign_id="6001",
                campaign_name="Campaign",
                impressions=0,
                clicks=0,
                cost_micros=0,
                has_provider_row=provider_row,
            ),
        )
    return YandexCampaignDiagnosticsSnapshot(
        date_from="2026-08-03",
        date_to="2026-08-09",
        period_days=7,
        connected_accounts=connected,
        managed_campaigns=managed,
        impressions=0,
        clicks=0,
        cost_micros=cost,
        campaigns=campaigns,
    )


def test_campaign_formatter_covers_empty_zero_row_and_unknown_money_states() -> None:
    assert "ещё не подключён" in yandex._format_campaign_snapshot(
        _snapshot(connected=0, managed=0, cost=0)
    )
    assert "Пока нет кампаний" in yandex._format_campaign_snapshot(
        _snapshot(connected=1, managed=0, cost=0)
    )
    text = yandex._format_campaign_snapshot(
        _snapshot(connected=2, managed=1, cost=None, provider_row=False)
    )
    assert "пока 0 строк в отчёте" in text
    assert "Общий денежный итог скрыт" in text
    assert "Средний CPC: —" in text


@pytest.mark.asyncio
async def test_campaign_pending_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    callback = _Callback(f"cpy:c:{token}:7")
    state = _State()
    monkeypatch.setattr(
        yandex.control,
        "_actor",
        AsyncMock(return_value=SimpleNamespace(user_id=101, business_id=business_id)),
    )

    async def direct_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(yandex.asyncio, "to_thread", direct_to_thread)

    def pending(**_kwargs):
        raise YandexDirectError("analytics_report_pending", retryable=True)

    monkeypatch.setattr(yandex, "get_yandex_campaign_diagnostics", pending)
    await yandex.open_yandex_campaign_diagnostics(callback, state)

    assert state.cleared == 1
    assert "Яндекс готовит отчёт по кампаниям" in callback.answers[-1][0][0]


@pytest.mark.asyncio
async def test_campaign_provider_failure_and_invalid_callback_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    monkeypatch.setattr(
        yandex.control,
        "_actor",
        AsyncMock(return_value=SimpleNamespace(user_id=101, business_id=business_id)),
    )

    async def direct_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(yandex.asyncio, "to_thread", direct_to_thread)

    def failed(**_kwargs):
        raise YandexDirectError("analytics_report_failed")

    monkeypatch.setattr(yandex, "get_yandex_campaign_diagnostics", failed)
    callback = _Callback(f"cpy:c:{token}:30")
    state = _State()
    await yandex.open_yandex_campaign_diagnostics(callback, state)
    assert state.cleared == 1
    assert "Проверьте подключение кабинета" in callback.answers[-1][0][0]

    malformed = _Callback(f"cpy:c:{token}:14")
    untouched = _State()
    await yandex.open_yandex_campaign_diagnostics(malformed, untouched)
    assert untouched.cleared == 0
    assert malformed.answers[-1][0] == ("Статистика Яндекса сейчас недоступна.",)


@pytest.mark.asyncio
async def test_campaign_permission_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    callback = _Callback(f"cpy:c:{token}:7")
    state = _State()
    monkeypatch.setattr(
        yandex.control,
        "_actor",
        AsyncMock(side_effect=PermissionError("foreign tenant")),
    )
    await yandex.open_yandex_campaign_diagnostics(callback, state)
    assert state.cleared == 1
    assert callback.answers[-1][0] == ("Статистика Яндекса сейчас недоступна.",)
