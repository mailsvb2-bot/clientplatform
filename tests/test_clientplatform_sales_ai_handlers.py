from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from handlers import clientplatform_sales as sales


class _State:
    def __init__(self) -> None:
        self.cleared = 0

    async def clear(self) -> None:
        self.cleared += 1


class _Message:
    def __init__(self) -> None:
        self.answers: list[tuple[str, dict[str, object]]] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append((text, kwargs))


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=101)
        self.message = _Message()
        self.answers: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def answer(self, *args: object, **kwargs: object) -> None:
        self.answers.append((args, kwargs))


@pytest.fixture
def ids() -> tuple[str, str, str]:
    business_id = str(uuid4())
    lead_id = str(uuid4())
    return business_id, lead_id, sales._token(business_id)


def _patch_control(monkeypatch: pytest.MonkeyPatch, actor: object) -> None:
    monkeypatch.setattr(sales.control, "_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(sales.control, "_callback_message", lambda callback: callback.message)
    monkeypatch.setattr(sales.control, "_keyboard", lambda rows: rows)


async def _inline_to_thread(func, /, *args, **kwargs):
    return func(*args, **kwargs)


@pytest.mark.asyncio
async def test_toggle_sales_ai_fails_closed_when_runtime_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, ids: tuple[str, str, str]
) -> None:
    business_id, _lead_id, token = ids
    callback = _Callback(f"cps:sat:{token}")
    state = _State()
    _patch_control(monkeypatch, SimpleNamespace(business_id=business_id))

    from clientplatform.application import sales_ai_drafts

    monkeypatch.setattr(sales_ai_drafts, "sales_ai_runtime_available", lambda: False)

    await sales.toggle_sales_ai(callback, state)

    assert state.cleared == 0
    assert callback.answers[-1] == (("ИИ сейчас не настроен на сервере",), {"show_alert": True})


@pytest.mark.asyncio
async def test_toggle_sales_ai_disables_existing_consent_and_refreshes_work(
    monkeypatch: pytest.MonkeyPatch, ids: tuple[str, str, str]
) -> None:
    business_id, _lead_id, token = ids
    actor = SimpleNamespace(business_id=business_id)
    callback = _Callback(f"cps:sat:{token}")
    state = _State()
    _patch_control(monkeypatch, actor)
    monkeypatch.setattr(sales.asyncio, "to_thread", _inline_to_thread)
    refresh = AsyncMock()
    monkeypatch.setattr(sales, "_send_sales_work", refresh)

    from clientplatform.application import sales_ai_drafts, sales_ai_settings

    monkeypatch.setattr(sales_ai_drafts, "sales_ai_runtime_available", lambda: True)
    monkeypatch.setattr(sales_ai_settings, "get_business_sales_ai_enabled", lambda *, actor: True)
    changes: list[tuple[object, bool]] = []
    monkeypatch.setattr(
        sales_ai_settings,
        "set_business_sales_ai_enabled",
        lambda *, actor, enabled, **_kwargs: changes.append((actor, enabled)) or enabled,
    )

    await sales.toggle_sales_ai(callback, state)

    assert state.cleared == 1
    assert changes == [(actor, False)]
    assert callback.answers[-1][0] == ("ИИ-помощник выключен",)
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_toggle_sales_ai_shows_provider_bound_consent_before_enabling(
    monkeypatch: pytest.MonkeyPatch, ids: tuple[str, str, str]
) -> None:
    business_id, _lead_id, token = ids
    actor = SimpleNamespace(business_id=business_id)
    callback = _Callback(f"cps:sat:{token}")
    state = _State()
    _patch_control(monkeypatch, actor)
    monkeypatch.setattr(sales.asyncio, "to_thread", _inline_to_thread)

    from clientplatform.application import sales_ai_drafts, sales_ai_settings

    monkeypatch.setattr(sales_ai_drafts, "sales_ai_runtime_available", lambda: True)
    monkeypatch.setattr(sales_ai_drafts, "sales_ai_runtime_provider_label", lambda: "DeepSeek")
    monkeypatch.setattr(
        sales_ai_drafts,
        "sales_ai_runtime_consent_target",
        lambda: "deepseek:https://api.deepseek.com",
    )
    monkeypatch.setattr(sales_ai_settings, "get_business_sales_ai_enabled", lambda *, actor: False)

    await sales.toggle_sales_ai(callback, state)

    assert state.cleared == 1
    assert callback.answers[-1] == ((), {})
    text, kwargs = callback.message.answers[-1]
    assert "DeepSeek" in text
    assert "deepseek:https://api.deepseek.com" in text
    assert "не получает права отправлять сообщения" in text
    buttons = kwargs["reply_markup"]
    assert buttons[0][0][1] == f"cps:sae:{token}"


@pytest.mark.asyncio
async def test_enable_sales_ai_uses_redacted_mode_and_notice_confirmation(
    monkeypatch: pytest.MonkeyPatch, ids: tuple[str, str, str]
) -> None:
    business_id, _lead_id, token = ids
    actor = SimpleNamespace(business_id=business_id)
    callback = _Callback(f"cps:sae:{token}")
    state = _State()
    _patch_control(monkeypatch, actor)
    monkeypatch.setattr(sales.asyncio, "to_thread", _inline_to_thread)
    refresh = AsyncMock()
    monkeypatch.setattr(sales, "_send_sales_work", refresh)

    from clientplatform.application import sales_ai_drafts, sales_ai_settings

    monkeypatch.setattr(sales_ai_drafts, "sales_ai_runtime_available", lambda: True)
    captured: dict[str, object] = {}

    def enable(**kwargs: object) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setattr(sales_ai_settings, "set_business_sales_ai_enabled", enable)

    await sales.enable_sales_ai(callback, state)

    assert state.cleared == 1
    assert captured["actor"] is actor
    assert captured["enabled"] is True
    assert captured["data_mode"] == "redacted"
    assert captured["customer_notice_confirmed"] is True
    assert callback.answers[-1][0] == ("ИИ-помощник включён",)
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_draft_sales_answer_shows_review_only_draft(
    monkeypatch: pytest.MonkeyPatch, ids: tuple[str, str, str]
) -> None:
    business_id, lead_id, token = ids
    actor = SimpleNamespace(business_id=business_id)
    callback = _Callback(f"cps:sad:{token}:{sales._token(lead_id)}")
    state = _State()
    _patch_control(monkeypatch, actor)

    from clientplatform.application import sales_ai_drafts

    monkeypatch.setattr(
        sales_ai_drafts,
        "draft_sales_reply",
        AsyncMock(return_value=SimpleNamespace(text="Предлагаю обсудить аудит.")),
    )

    await sales.draft_sales_answer(callback, state)

    assert state.cleared == 1
    assert callback.answers[0][0] == ("Готовлю черновик…",)
    text, _kwargs = callback.message.answers[-1]
    assert "Предлагаю обсудить аудит." in text
    assert "ничего не отправил клиенту автоматически" in text
