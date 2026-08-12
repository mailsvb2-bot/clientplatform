from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from clientplatform.application.visual_brand_discovery import (
    VisualBrandDiscoveryError,
    WebsiteBrandSuggestion,
)
from clientplatform.domain.tenancy import TenantPermissionDenied
from clientplatform.domain.visual_brand import TenantBrandDNA
from handlers import clientplatform_visual_brand as visual_brand


class _State:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})
        self.current_state = None
        self.cleared = False

    async def get_data(self) -> dict:
        return dict(self.data)

    async def set_data(self, data: dict) -> None:
        self.data = dict(data)

    async def update_data(self, **values) -> None:
        self.data.update(values)

    async def set_state(self, value) -> None:
        self.current_state = value

    async def clear(self) -> None:
        self.data = {}
        self.current_state = None
        self.cleared = True


class _Message:
    def __init__(self, text: str = "", *, user_id: int = 17) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append((text, kwargs))


class _Callback:
    def __init__(self, data: str, *, user_id: int = 17) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str | None, bool]] = []
        self.message = object()

    async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def _run(coro) -> None:
    asyncio.run(coro)


def _brand(business_id: str, *, display_name: str = "North Star") -> TenantBrandDNA:
    return TenantBrandDNA(
        business_id=business_id,
        display_name=display_name,
        tone=("human", "trustworthy"),
        visual_keywords=("editorial", "calm"),
        forbidden_visuals=("fake reviews", "invented statistics"),
        primary_color="#112233",
        accent_color="#DDAA44",
        text_color="#FFFFFF",
    ).normalized()


def _install_actor(monkeypatch, business_id: str, actor: object | None = None) -> object:
    resolved = actor or SimpleNamespace(business_id=business_id)

    async def get_actor(user_id: int, requested_business_id: str):
        assert user_id == 17
        assert requested_business_id == business_id
        return resolved

    monkeypatch.setattr(visual_brand.control, "_actor", get_actor)
    return resolved


def _install_callback_message(monkeypatch) -> _Message:
    target = _Message()
    monkeypatch.setattr(visual_brand.control, "_callback_message", lambda _callback: target)
    return target


def test_brand_helpers_preserve_safety_and_parse_manual_changes() -> None:
    business_id = str(uuid4())
    current = _brand(business_id)

    text = visual_brand._brand_text(current)
    assert "North Star" in text
    assert "human, trustworthy" in text
    assert "editorial, calm" in text
    assert "#112233" in text

    changed = visual_brand._manual_brand(
        current,
        "ignored line\n"
        "Название: New Practice\n"
        "Основной цвет: #010203\n"
        "Акцентный цвет: #AABBCC\n"
        "Цвет текста: #F1F2F3\n"
        "Визуальный стиль: modern; warm, human\n"
        "Неизвестно: ничего",
    )
    assert changed.display_name == "New Practice"
    assert changed.primary_color == "#010203"
    assert changed.accent_color == "#AABBCC"
    assert changed.text_color == "#F1F2F3"
    assert changed.visual_keywords == ("modern", "warm", "human")
    assert changed.tone == current.tone
    assert changed.forbidden_visuals == current.forbidden_visuals

    with pytest.raises(ValueError, match="not recognized"):
        visual_brand._manual_brand(current, "обычный текст без полей")


def test_brand_from_state_requires_proposal_and_normalizes_it() -> None:
    business_id = str(uuid4())
    source = _brand(business_id)

    with pytest.raises(ValueError, match="proposal is unavailable"):
        visual_brand._brand_from_state({}, business_id)

    restored = visual_brand._brand_from_state(
        {
            "brand_proposal": {
                "display_name": source.display_name,
                "tone": list(source.tone),
                "visual_keywords": list(source.visual_keywords),
                "forbidden_visuals": list(source.forbidden_visuals),
                "primary_color": source.primary_color,
                "accent_color": source.accent_color,
                "text_color": source.text_color,
            }
        },
        business_id,
    )
    assert restored == source


def test_open_visual_brand_shows_current_profile(monkeypatch) -> None:
    business_id = str(uuid4())
    actor = _install_actor(monkeypatch, business_id)
    target = _install_callback_message(monkeypatch)
    monkeypatch.setattr(visual_brand, "_business_id", lambda _token: business_id)
    monkeypatch.setattr(visual_brand, "load_goal_visual_brand", lambda *, actor: _brand(business_id))
    callback = _Callback("cpb:open:token")
    state = _State({"old": True})

    _run(visual_brand.open_visual_brand(callback, state))

    assert state.cleared is True
    assert callback.answers == [(None, False)]
    assert "Фирменный стиль ClientPlatform" in target.answers[0][0]
    assert "North Star" in target.answers[0][0]
    markup = target.answers[0][1]["reply_markup"]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert callbacks == ["cpb:site:token", "cpb:manual:token", "cpj:home:token"]
    assert actor is not None


def test_open_visual_brand_fails_closed_for_invalid_business(monkeypatch) -> None:
    monkeypatch.setattr(
        visual_brand,
        "_business_id",
        lambda _token: (_ for _ in ()).throw(ValueError("bad token")),
    )
    callback = _Callback("cpb:open:bad")

    _run(visual_brand.open_visual_brand(callback, _State()))

    assert callback.answers == [("Не удалось открыть фирменный стиль", True)]


def test_ask_brand_website_starts_confirmed_discovery_state(monkeypatch) -> None:
    business_id = str(uuid4())
    _install_actor(monkeypatch, business_id)
    target = _install_callback_message(monkeypatch)
    monkeypatch.setattr(visual_brand, "_business_id", lambda _token: business_id)
    callback = _Callback("cpb:site:token")
    state = _State()

    _run(visual_brand.ask_brand_website(callback, state))

    assert state.current_state == visual_brand.VisualBrandState.waiting_website
    assert state.data == {
        "brand_business_id": business_id,
        "brand_business_token": "token",
    }
    assert callback.answers == [(None, False)]
    assert "ничего не сохранит" in target.answers[0][0]
    assert "Внутренние и локальные адреса блокируются" in target.answers[0][0]


def test_ask_brand_website_rejects_unavailable_business(monkeypatch) -> None:
    business_id = str(uuid4())
    monkeypatch.setattr(visual_brand, "_business_id", lambda _token: business_id)

    async def denied(_user_id: int, _business_id: str):
        raise TenantPermissionDenied("denied")

    monkeypatch.setattr(visual_brand.control, "_actor", denied)
    callback = _Callback("cpb:site:token")

    _run(visual_brand.ask_brand_website(callback, _State()))

    assert callback.answers == [("Бизнес недоступен", True)]


def test_receive_brand_website_builds_proposal_without_saving(monkeypatch) -> None:
    business_id = str(uuid4())
    current = _brand(business_id)
    proposal = TenantBrandDNA(
        business_id=business_id,
        display_name="Site Brand",
        tone=current.tone,
        visual_keywords=("editorial", "modern"),
        forbidden_visuals=current.forbidden_visuals,
        primary_color="#224466",
        accent_color=current.accent_color,
        text_color=current.text_color,
    ).normalized()
    _install_actor(monkeypatch, business_id)
    monkeypatch.setattr(visual_brand.control, "_user_id", lambda _message: 17)
    monkeypatch.setattr(visual_brand, "load_goal_visual_brand", lambda *, actor: current)
    monkeypatch.setattr(
        visual_brand,
        "discover_brand_from_website",
        lambda **_kwargs: WebsiteBrandSuggestion(
            source_url="https://example.com/",
            brand=proposal,
            changed_fields=("display_name", "primary_color"),
            evidence=("site_name", "theme_color"),
        ),
    )
    state = _State(
        {
            "brand_business_id": business_id,
            "brand_business_token": "token",
        }
    )
    message = _Message("https://example.com/")

    _run(visual_brand.receive_brand_website(message, state))

    assert state.current_state == visual_brand.VisualBrandState.confirming
    assert state.data["brand_proposal"]["display_name"] == "Site Brand"
    assert state.data["brand_proposal_source"] == "https://example.com/"
    assert state.data["brand_proposal_evidence"] == ["site_name", "theme_color"]
    assert "Пока это только предложение" in message.answers[0][0]
    assert "site_name, theme_color" in message.answers[0][0]


def test_receive_brand_website_no_strong_changes_keeps_profile(monkeypatch) -> None:
    business_id = str(uuid4())
    current = _brand(business_id)
    _install_actor(monkeypatch, business_id)
    monkeypatch.setattr(visual_brand.control, "_user_id", lambda _message: 17)
    monkeypatch.setattr(visual_brand, "load_goal_visual_brand", lambda *, actor: current)
    monkeypatch.setattr(
        visual_brand,
        "discover_brand_from_website",
        lambda **_kwargs: WebsiteBrandSuggestion(
            source_url="https://example.com/",
            brand=current,
            changed_fields=(),
            evidence=(),
        ),
    )
    state = _State(
        {
            "brand_business_id": business_id,
            "brand_business_token": "token",
        }
    )
    message = _Message("https://example.com/")

    _run(visual_brand.receive_brand_website(message, state))

    assert state.cleared is True
    assert "оставил без изменений" in message.answers[0][0]
    assert "reply_markup" in message.answers[0][1]


def test_receive_brand_website_handles_expired_unsafe_and_invalid_sessions(monkeypatch) -> None:
    expired = _State()
    expired_message = _Message("https://example.com/")
    _run(visual_brand.receive_brand_website(expired_message, expired))
    assert expired.cleared is True
    assert "Сессия настройки устарела" in expired_message.answers[0][0]

    business_id = str(uuid4())
    _install_actor(monkeypatch, business_id)
    monkeypatch.setattr(visual_brand.control, "_user_id", lambda _message: 17)
    monkeypatch.setattr(visual_brand, "load_goal_visual_brand", lambda *, actor: _brand(business_id))
    base_data = {
        "brand_business_id": business_id,
        "brand_business_token": "token",
    }

    monkeypatch.setattr(
        visual_brand,
        "discover_brand_from_website",
        lambda **_kwargs: (_ for _ in ()).throw(
            VisualBrandDiscoveryError("brand_website_private_address_forbidden")
        ),
    )
    unsafe_message = _Message("http://127.0.0.1/")
    _run(visual_brand.receive_brand_website(unsafe_message, _State(base_data)))
    assert "Не удалось безопасно прочитать" in unsafe_message.answers[0][0]

    monkeypatch.setattr(
        visual_brand,
        "discover_brand_from_website",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("invalid")),
    )
    invalid_message = _Message("https://example.com/")
    _run(visual_brand.receive_brand_website(invalid_message, _State(base_data)))
    assert "Не удалось подготовить предложение" in invalid_message.answers[0][0]


def test_manual_brand_flow_previews_changes_and_preserves_safety(monkeypatch) -> None:
    business_id = str(uuid4())
    current = _brand(business_id)
    _install_actor(monkeypatch, business_id)
    target = _install_callback_message(monkeypatch)
    monkeypatch.setattr(visual_brand, "_business_id", lambda _token: business_id)
    callback = _Callback("cpb:manual:token")
    state = _State()

    _run(visual_brand.ask_manual_brand(callback, state))
    assert state.current_state == visual_brand.VisualBrandState.waiting_manual
    assert "Сначала покажу результат" in target.answers[0][0]

    monkeypatch.setattr(visual_brand.control, "_user_id", lambda _message: 17)
    monkeypatch.setattr(visual_brand, "load_goal_visual_brand", lambda *, actor: current)
    message = _Message(
        "Название: New Brand\n"
        "Основной: #334455\n"
        "Акцент: #BB8844\n"
        "Текст: #FAFAFA\n"
        "Стиль: premium, calm"
    )
    _run(visual_brand.receive_manual_brand(message, state))

    assert state.current_state == visual_brand.VisualBrandState.confirming
    proposal = state.data["brand_proposal"]
    assert proposal["display_name"] == "New Brand"
    assert proposal["forbidden_visuals"] == current.forbidden_visuals
    assert proposal["visual_keywords"] == ("premium", "calm")
    assert state.data["brand_proposal_source"] == "manual"
    assert "Изменения ещё не сохранены" in message.answers[0][0]


def test_manual_brand_flow_handles_denied_expired_and_invalid_input(monkeypatch) -> None:
    business_id = str(uuid4())
    monkeypatch.setattr(visual_brand, "_business_id", lambda _token: business_id)

    async def denied(_user_id: int, _business_id: str):
        raise TenantPermissionDenied("denied")

    monkeypatch.setattr(visual_brand.control, "_actor", denied)
    callback = _Callback("cpb:manual:token")
    _run(visual_brand.ask_manual_brand(callback, _State()))
    assert callback.answers == [("Бизнес недоступен", True)]

    expired_message = _Message("Название: New")
    expired_state = _State()
    _run(visual_brand.receive_manual_brand(expired_message, expired_state))
    assert expired_state.cleared is True
    assert "Сессия настройки устарела" in expired_message.answers[0][0]

    _install_actor(monkeypatch, business_id)
    monkeypatch.setattr(visual_brand.control, "_user_id", lambda _message: 17)
    monkeypatch.setattr(
        visual_brand,
        "load_goal_visual_brand",
        lambda *, actor: _brand(business_id),
    )
    invalid_message = _Message("неизвестное поле без значения")
    state = _State(
        {
            "brand_business_id": business_id,
            "brand_business_token": "token",
        }
    )
    _run(visual_brand.receive_manual_brand(invalid_message, state))
    assert "Не смог распознать настройки" in invalid_message.answers[0][0]


def test_apply_visual_brand_requires_fresh_proposal_and_manager_permission(monkeypatch) -> None:
    business_id = str(uuid4())
    proposal = _brand(business_id, display_name="Confirmed")
    callback = _Callback("cpb:apply:token")

    stale = _State({"brand_business_token": "other"})
    _run(visual_brand.apply_visual_brand(callback, stale))
    assert callback.answers == [("Это предложение уже устарело", True)]

    monkeypatch.setattr(visual_brand, "_business_id", lambda _token: business_id)

    async def denied(_user_id: int, _business_id: str):
        raise TenantPermissionDenied("denied")

    monkeypatch.setattr(visual_brand.control, "_actor", denied)
    denied_callback = _Callback("cpb:apply:token")
    denied_state = _State(
        {
            "brand_business_token": "token",
            "brand_proposal": {
                "display_name": proposal.display_name,
                "tone": list(proposal.tone),
                "visual_keywords": list(proposal.visual_keywords),
                "forbidden_visuals": list(proposal.forbidden_visuals),
                "primary_color": proposal.primary_color,
                "accent_color": proposal.accent_color,
                "text_color": proposal.text_color,
            },
        }
    )
    _run(visual_brand.apply_visual_brand(denied_callback, denied_state))
    assert denied_callback.answers == [
        ("Сохранять фирменный стиль может владелец или администратор", True)
    ]


def test_apply_visual_brand_saves_confirmed_identity_and_handles_invalid_proposal(monkeypatch) -> None:
    business_id = str(uuid4())
    proposal = _brand(business_id, display_name="Confirmed")
    actor = _install_actor(monkeypatch, business_id)
    target = _install_callback_message(monkeypatch)
    monkeypatch.setattr(visual_brand, "_business_id", lambda _token: business_id)
    saved_calls: list[tuple[object, TenantBrandDNA]] = []

    def save(*, actor, brand):
        saved_calls.append((actor, brand))
        return brand

    monkeypatch.setattr(visual_brand, "save_goal_visual_brand", save)
    state = _State(
        {
            "brand_business_token": "token",
            "brand_proposal": {
                "display_name": proposal.display_name,
                "tone": list(proposal.tone),
                "visual_keywords": list(proposal.visual_keywords),
                "forbidden_visuals": list(proposal.forbidden_visuals),
                "primary_color": proposal.primary_color,
                "accent_color": proposal.accent_color,
                "text_color": proposal.text_color,
            },
        }
    )
    callback = _Callback("cpb:apply:token")

    _run(visual_brand.apply_visual_brand(callback, state))

    assert state.cleared is True
    assert callback.answers == [("Сохранено", False)]
    assert saved_calls == [(actor, proposal)]
    assert "Brand DNA сохранён" in target.answers[0][0]
    assert "Confirmed" in target.answers[0][0]

    bad_callback = _Callback("cpb:apply:token")
    bad_state = _State(
        {
            "brand_business_token": "token",
            "brand_proposal": {"primary_color": "not-a-color"},
        }
    )
    _run(visual_brand.apply_visual_brand(bad_callback, bad_state))
    assert bad_callback.answers == [("Не удалось сохранить фирменный стиль", True)]


def test_cancel_visual_brand_discards_pending_proposal(monkeypatch) -> None:
    target = _install_callback_message(monkeypatch)
    callback = _Callback("cpb:cancel:token")
    state = _State({"brand_proposal": {"display_name": "Pending"}})

    _run(visual_brand.cancel_visual_brand(callback, state))

    assert state.cleared is True
    assert callback.answers == [("Изменения не сохранены", False)]
    assert "Оставил текущий фирменный стиль без изменений" in target.answers[0][0]
    assert "reply_markup" in target.answers[0][1]
