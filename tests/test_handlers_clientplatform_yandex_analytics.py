from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.methods import AnswerCallbackQuery

from clientplatform.application.yandex_campaign_diagnostics import (
    YandexCampaignDiagnosticsRow,
    YandexCampaignDiagnosticsSnapshot,
)
from clientplatform.application.yandex_growth_analytics import (
    YandexGrowthCampaignSnapshot,
    YandexGrowthSnapshot,
)
from clientplatform.integrations.yandex_direct import YandexDirectError

yandex = importlib.import_module("handlers.clientplatform_yandex_analytics")
promotion_install = importlib.import_module("handlers.clientplatform_promotion_install")
control = importlib.import_module("handlers.clientplatform_control")


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, user_id: int = 101) -> None:
        self.from_user = FakeUser(user_id)
        self.answers: list[tuple[str, dict[str, Any]]] = []
        self.edits: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append((text, kwargs))


class FakeCallback:
    def __init__(self, data: str, user_id: int = 101) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(user_id)
        self.answers: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))


class FakeState:
    def __init__(self) -> None:
        self.clear_count = 0

    async def clear(self) -> None:
        self.clear_count += 1


async def direct_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def direct_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yandex.asyncio, "to_thread", direct_to_thread)
    monkeypatch.setattr(
        yandex.control,
        "_actor",
        AsyncMock(
            side_effect=lambda user_id, business_id: SimpleNamespace(
                user_id=user_id,
                business_id=business_id,
            )
        ),
    )
    monkeypatch.setattr(yandex.control, "_callback_message", lambda callback: callback.message)
    monkeypatch.setattr(yandex, "screen_code_configuration_available", lambda: True)


def _snapshot(*, connected_accounts: int = 1, tracked_ads: int = 2) -> YandexGrowthSnapshot:
    campaign = YandexGrowthCampaignSnapshot(
        connection_id=str(uuid4()),
        campaign_id="6001",
        campaign_name="Консультация — август",
        tracked_ads=tracked_ads,
        impressions=1000,
        clicks=100,
        cost_micros=50_000_000,
        leads=10,
        bookings=5,
        won=2,
    )
    return YandexGrowthSnapshot(
        date_from="2026-07-11",
        date_to="2026-08-09",
        period_days=30,
        connected_accounts=connected_accounts,
        tracked_ads=tracked_ads,
        impressions=1000 if tracked_ads else 0,
        clicks=100 if tracked_ads else 0,
        cost_micros=50_000_000 if tracked_ads else 0,
        leads=10 if tracked_ads else 0,
        bookings=5 if tracked_ads else 0,
        won=2 if tracked_ads else 0,
        campaigns=(campaign,) if tracked_ads else (),
    )


def _campaign_snapshot() -> YandexCampaignDiagnosticsSnapshot:
    row = YandexCampaignDiagnosticsRow(
        connection_id=str(uuid4()),
        campaign_id="6001",
        campaign_name="Консультация — август",
        impressions=800,
        clicks=40,
        cost_micros=20_000_000,
        has_provider_row=True,
    )
    return YandexCampaignDiagnosticsSnapshot(
        date_from="2026-07-11",
        date_to="2026-08-09",
        period_days=30,
        connected_accounts=1,
        managed_campaigns=1,
        impressions=800,
        clicks=40,
        cost_micros=20_000_000,
        campaigns=(row,),
    )


def test_owner_dashboard_exposes_yandex_analytics() -> None:
    business_id = str(uuid4())
    markup = promotion_install._owner_keyboard(control, business_id)
    buttons = [button for row in markup.inline_keyboard for button in row]
    yandex_button = next(button for button in buttons if button.text == "📊 Яндекс")
    assert yandex_button.callback_data == f"cpy:a:{control._uuid_token(business_id)}:30"
    assert len(str(yandex_button.callback_data).encode("utf-8")) <= 64


def test_snapshot_copy_is_evidence_only_and_no_romi_guess() -> None:
    text = yandex._format_snapshot(_snapshot())
    assert "Только объявления ClientPlatform по точным Yandex AdId" in text
    assert "Показы: 1000" in text
    assert "Клики: 100" in text
    assert "CTR: 10.0%" in text
    assert "Расход: 50.00 в валюте кабинета" in text
    assert "Средний CPC: 0.50" in text
    assert "Лиды по измеряемым ссылкам: 10" in text
    assert "CPL: 5.00" in text
    assert "Стоимость записи: 10.00" in text
    assert "CAC: 25.00" in text
    assert "Консультация — август" in text
    assert "Выручка и ROMI не показываются" in text


def test_campaign_copy_is_explicitly_diagnostics_not_attribution() -> None:
    text = yandex._format_campaign_snapshot(_campaign_snapshot())
    assert "CampaignId диагностика" in text
    assert "[6001]" in text
    assert "800 показов" in text
    assert "40 кликов" in text
    assert "20.00 в валюте кабинета" in text
    assert "не атрибуция лидов, записей или выручки" in text
    assert "CampaignId-расход к выручке автоматически не приписывается" in text


def test_empty_states_do_not_invent_provider_metrics() -> None:
    not_connected = yandex._format_snapshot(_snapshot(connected_accounts=0, tracked_ads=0))
    assert "Рекламный кабинет ещё не подключён" in not_connected
    assert "Подключить Яндекс Директ" in not_connected
    assert "OAuth-токен будет храниться зашифрованно" in not_connected

    no_tracked_ads = yandex._format_snapshot(_snapshot(connected_accounts=1, tracked_ads=0))
    assert "Подключённых кабинетов: 1" in no_tracked_ads
    assert "CPL/CAC не придумываются" in no_tracked_ads

    assert yandex._money(None) == "—"
    assert yandex._metric("CAC", None) == "CAC: —"


def test_not_connected_keyboard_has_direct_connect_and_no_period_noise() -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    markup = yandex._keyboard(
        business_id,
        30,
        connected_accounts=0,
        connect_available=True,
    )
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert [button.text for button in buttons] == [
        "➕ Подключить Яндекс Директ",
        "📣 Рекламные кабинеты",
        "← Получать клиентов",
    ]
    assert buttons[0].callback_data == f"cpa:connect:{token}"
    assert all(
        button.text not in {"7 дней", "30 дней", "✅ 7 дней", "✅ 30 дней"}
        for button in buttons
    )


def test_disabled_yandex_configuration_hides_dead_connect_action() -> None:
    business_id = str(uuid4())
    markup = yandex._keyboard(
        business_id,
        30,
        connected_accounts=0,
        connect_available=False,
    )
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "➕ Подключить Яндекс Директ" not in labels
    assert labels == ["📣 Рекламные кабинеты", "← Получать клиентов"]
    text = yandex._format_snapshot(
        _snapshot(connected_accounts=0, tracked_ads=0),
        connect_available=False,
    )
    assert "отключено или не настроено администратором" in text
    assert "Нажмите «Подключить" not in text


def test_mixed_account_money_is_explicitly_hidden() -> None:
    snapshot = _snapshot()
    snapshot = YandexGrowthSnapshot(
        date_from=snapshot.date_from,
        date_to=snapshot.date_to,
        period_days=snapshot.period_days,
        connected_accounts=2,
        tracked_ads=snapshot.tracked_ads,
        impressions=snapshot.impressions,
        clicks=snapshot.clicks,
        cost_micros=None,
        leads=snapshot.leads,
        bookings=snapshot.bookings,
        won=snapshot.won,
        campaigns=snapshot.campaigns,
    )
    text = yandex._format_snapshot(snapshot)
    assert "Расход: —" in text
    assert "Средний CPC: —" in text
    assert "CPL: —" in text
    assert "CAC: —" in text
    assert "Денежные итоги не складываются" in text


@pytest.mark.asyncio
async def test_owner_can_open_30_day_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    monkeypatch.setattr(yandex, "get_yandex_growth_snapshot", lambda **_kwargs: _snapshot())
    callback = FakeCallback(f"cpy:a:{token}:30")
    state = FakeState()

    await yandex.open_yandex_analytics(callback, state)

    assert state.clear_count == 1
    assert callback.answers == []
    assert callback.message.answers == []
    text, kwargs = callback.message.edits[-1]
    assert "📊 Яндекс Директ" in text
    labels = [
        button.text
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert labels == [
        "7 дней",
        "✅ 30 дней",
        "📈 Кампании по CampaignId",
        "📣 Рекламные кабинеты",
        "← Получать клиентов",
    ]


@pytest.mark.asyncio
async def test_owner_can_open_campaign_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    monkeypatch.setattr(
        yandex,
        "get_yandex_campaign_diagnostics",
        lambda **_kwargs: _campaign_snapshot(),
    )
    callback = FakeCallback(f"cpy:c:{token}:30")
    state = FakeState()

    await yandex.open_yandex_campaign_diagnostics(callback, state)

    assert state.clear_count == 1
    assert callback.answers == []
    text, kwargs = callback.message.edits[-1]
    assert "CampaignId диагностика" in text
    labels = [
        button.text
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert labels == [
        "7 дней",
        "✅ 30 дней",
        "🎯 Exact AdId + результаты",
        "📣 Рекламные кабинеты",
        "← Получать клиентов",
    ]


@pytest.mark.asyncio
async def test_period_navigation_clears_active_wizard_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    state = FakeState()

    def snapshot_after_escape(**_kwargs: Any) -> YandexGrowthSnapshot:
        assert state.clear_count == 1
        return _snapshot()

    monkeypatch.setattr(yandex, "get_yandex_growth_snapshot", snapshot_after_escape)
    callback = FakeCallback(f"cpy:a:{token}:7")

    await yandex.open_yandex_analytics(callback, state)

    assert state.clear_count == 1
    assert callback.answers == []
    assert callback.message.edits


@pytest.mark.asyncio
async def test_expired_callback_feedback_falls_back_to_normal_message() -> None:
    callback = FakeCallback("cpy:a:ignored:7")
    callback.answer = AsyncMock(
        side_effect=yandex.TelegramBadRequest(
            method=AnswerCallbackQuery(callback_query_id="expired"),
            message="Bad Request: query is too old",
        )
    )

    await yandex._answer_feedback(
        callback,
        "Яндекс готовит отчёт. Попробуйте ещё раз.",
        show_alert=True,
    )

    callback.answer.assert_awaited_once_with(
        "Яндекс готовит отчёт. Попробуйте ещё раз.",
        show_alert=True,
    )
    assert callback.message.answers == [
        ("Яндекс готовит отчёт. Попробуйте ещё раз.", {})
    ]


@pytest.mark.asyncio
async def test_not_connected_snapshot_replaces_owner_panel_with_connect_cta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    monkeypatch.setattr(
        yandex,
        "get_yandex_growth_snapshot",
        lambda **_kwargs: _snapshot(connected_accounts=0, tracked_ads=0),
    )
    callback = FakeCallback(f"cpy:a:{token}:30")

    await yandex.open_yandex_analytics(callback, FakeState())

    assert callback.message.answers == []
    text, kwargs = callback.message.edits[-1]
    assert "Рекламный кабинет ещё не подключён" in text
    labels = [
        button.text
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert labels[0] == "➕ Подключить Яндекс Директ"
    assert "✅ 30 дней" not in labels


@pytest.mark.asyncio
async def test_not_connected_snapshot_hides_cta_when_oauth_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)
    monkeypatch.setattr(
        yandex,
        "get_yandex_growth_snapshot",
        lambda **_kwargs: _snapshot(connected_accounts=0, tracked_ads=0),
    )
    monkeypatch.setattr(yandex, "screen_code_configuration_available", lambda: False)
    callback = FakeCallback(f"cpy:a:{token}:30")

    await yandex.open_yandex_analytics(callback, FakeState())

    text, kwargs = callback.message.edits[-1]
    assert "отключено или не настроено администратором" in text
    labels = [
        button.text
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "➕ Подключить Яндекс Директ" not in labels


@pytest.mark.asyncio
async def test_pending_report_is_explicit_and_sends_no_fake_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)

    def pending(**_kwargs: Any) -> YandexGrowthSnapshot:
        raise YandexDirectError("analytics_report_pending", retryable=True)

    monkeypatch.setattr(yandex, "get_yandex_growth_snapshot", pending)
    callback = FakeCallback(f"cpy:a:{token}:7")
    state = FakeState()

    await yandex.open_yandex_analytics(callback, state)

    assert state.clear_count == 1
    assert callback.message.answers == []
    assert callback.message.edits == []
    args, kwargs = callback.answers[-1]
    assert "Яндекс готовит отчёт" in args[0]
    assert kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_provider_and_input_errors_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = str(uuid4())
    token = control._uuid_token(business_id)

    def failed(**_kwargs: Any) -> YandexGrowthSnapshot:
        raise YandexDirectError("provider_http_401")

    monkeypatch.setattr(yandex, "get_yandex_growth_snapshot", failed)
    callback = FakeCallback(f"cpy:a:{token}:30")
    failed_state = FakeState()
    await yandex.open_yandex_analytics(callback, failed_state)
    assert failed_state.clear_count == 1
    assert "Проверьте подключение кабинета" in callback.answers[-1][0][0]

    def invalid_runtime_value(**_kwargs: Any) -> YandexGrowthSnapshot:
        raise ValueError("path-like or corrupted analytics configuration")

    monkeypatch.setattr(yandex, "get_yandex_growth_snapshot", invalid_runtime_value)
    invalid_runtime = FakeCallback(f"cpy:a:{token}:7")
    invalid_runtime_state = FakeState()
    await yandex.open_yandex_analytics(invalid_runtime, invalid_runtime_state)
    assert invalid_runtime_state.clear_count == 1
    assert invalid_runtime.answers[-1][0] == ("Статистика Яндекса сейчас недоступна.",)
    assert invalid_runtime.answers[-1][1]["show_alert"] is True

    bad = FakeCallback(f"cpy:a:{token}:14")
    bad_state = FakeState()
    await yandex.open_yandex_analytics(bad, bad_state)
    assert bad_state.clear_count == 0
    assert bad.answers[-1][0] == ("Статистика Яндекса сейчас недоступна.",)
    assert bad.answers[-1][1]["show_alert"] is True

    malformed = FakeCallback(f"cpy:a:{token}:7:unexpected")
    malformed_state = FakeState()
    await yandex.open_yandex_analytics(malformed, malformed_state)
    assert malformed_state.clear_count == 0
    assert malformed.answers[-1][0] == ("Статистика Яндекса сейчас недоступна.",)
    assert malformed.answers[-1][1]["show_alert"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
