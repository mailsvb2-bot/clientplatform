from __future__ import annotations

import json

from clientplatform.application import visual_aesthetic_director as director


class StubClient:
    def __init__(self, reply):
        self.reply = reply

    def chat(self, messages, temperature=0.0, max_tokens=0):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_director_drops_invented_discounts_and_brand(monkeypatch):
    monkeypatch.setenv("VISUAL_AESTHETIC_DIRECTOR_ENABLED", "1")
    response = json.dumps({
        "subject": "чашка кофе",
        "setting": "уютная кофейня",
        "light": "мягкий свет",
        "angle": "вид сверху",
        "style": "фотореализм",
        "palette": "коричневый",
        "action": "скидка 90%",
    }, ensure_ascii=False)
    result = director.direct_visual_aesthetics(
        owner_request="чашка кофе",
        kind="image",
        client=StubClient(response),
    )
    assert result is not None
    assert result["subject"] == "чашка кофе"
    assert result["action"] == ""


def test_director_degrades_without_provider(monkeypatch):
    monkeypatch.setenv("VISUAL_AESTHETIC_DIRECTOR_ENABLED", "1")
    assert director.direct_visual_aesthetics(
        owner_request="чашка кофе",
        kind="image",
        client=StubClient(RuntimeError("down")),
    ) is None


def test_director_rejects_missing_subject():
    assert director.sanitize_visual_direction(
        {"subject": "", "style": "фотореализм"},
        owner_context="кофе",
    ) is None


def test_director_is_bounded():
    result = director.sanitize_visual_direction(
        {"subject": "кофе", "setting": "мягкий свет " * 40, "style": "матовый " * 40},
        owner_context="кофе",
    )
    assert result is not None
    assert len(" ".join(v for v in result.values() if v)) <= 350


def test_director_stays_disabled_without_opt_in(monkeypatch):
    monkeypatch.delenv("VISUAL_AESTHETIC_DIRECTOR_ENABLED", raising=False)
    assert director.direct_visual_aesthetics(
        owner_request="чашка кофе", kind="image", client=StubClient("{}"),
    ) is None


def test_director_rejects_malformed_and_fenced_json(monkeypatch):
    monkeypatch.setenv("VISUAL_AESTHETIC_DIRECTOR_ENABLED", "1")
    assert director.direct_visual_aesthetics(
        owner_request="кофе", kind="image", client=StubClient("invalid json"),
    ) is None
    valid = director.direct_visual_aesthetics(
        owner_request="кофе", kind="image",
        client=StubClient('```json\n{"subject": "кофе", "light": "мягкий"}\n```'),
    )
    assert valid is not None
    assert valid["light"] == "мягкий"


def test_director_empty_request_and_no_provider(monkeypatch):
    monkeypatch.setenv("VISUAL_AESTHETIC_DIRECTOR_ENABLED", "1")
    assert director.direct_visual_aesthetics(owner_request="", kind="image", client=StubClient("{}")) is None
    monkeypatch.setattr(director.OpenAIClient, "from_settings", lambda: None)
    assert director.direct_visual_aesthetics(owner_request="кофе", kind="image") is None


def test_director_provider_setup_error_falls_back(monkeypatch):
    monkeypatch.setenv("VISUAL_AESTHETIC_DIRECTOR_ENABLED", "1")
    def broken():
        raise RuntimeError("provider temporarily unavailable")
    monkeypatch.setattr(director.OpenAIClient, "from_settings", broken)
    assert director.direct_visual_aesthetics(owner_request="кофе", kind="image") is None


def test_direction_validation_rejects_unknown_fields_and_brand_hallucination():
    assert director.sanitize_visual_direction(
        {"subject": "кофе", "price": "500"}, owner_context="кофе",
    ) is None
    assert director.sanitize_visual_direction("coffee", owner_context="кофе") is None
    assert director.invents_fact("ACME", owner_context="кофе")
    assert not director.invents_fact("чашка кофе", owner_context="чашка кофе")


def test_provider_returns_null_or_empty_response(monkeypatch):
    monkeypatch.setenv("VISUAL_AESTHETIC_DIRECTOR_ENABLED", "1")
    assert director.direct_visual_aesthetics(
        owner_request="кофе", kind="image", client=StubClient(None),
    ) is None
    assert director.direct_visual_aesthetics(
        owner_request="кофе", kind="image", client=StubClient("{}"),
    ) is None


def test_director_accepts_owner_factual_claim_only_when_present():
    assert director.invents_fact("скидка 25%", owner_context="кофе")
    assert not director.invents_fact("скидка 25%", owner_context="скидка 25%")
    assert director.sanitize_visual_direction(None, owner_context="кофе") is None
    assert director.sanitize_visual_direction({}, owner_context="кофе") is None


def test_direction_preserves_fields_when_owner_supplies_language_and_numbers():
    result = director.sanitize_visual_direction(
        {"subject": "товар 2026", "setting": "studio", "light": "мягкий"},
        owner_context="товар 2026 studio",
    )
    assert result is not None
    assert result["subject"] == "товар 2026"
    assert result["setting"] == "studio"


def test_director_explicitly_disabled_flag_does_not_call_provider(monkeypatch):
    monkeypatch.setenv("VISUAL_AESTHETIC_DIRECTOR_ENABLED", "false")
    class ExplodingClient:
        def chat(self, *args, **kwargs):
            raise AssertionError("disabled director must not call provider")
    assert director.direct_visual_aesthetics(
        owner_request="кофе", kind="image", client=ExplodingClient(),
    ) is None


def test_director_bounded_long_owner_input(monkeypatch):
    monkeypatch.setenv("VISUAL_AESTHETIC_DIRECTOR_ENABLED", "1")
    class InspectingClient:
        def chat(self, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            assert len(payload["request"]) <= 120
            return '{"subject":"кофе"}'
    result = director.direct_visual_aesthetics(
        owner_request="кофе " * 500, kind="image", client=InspectingClient(),
    )
    assert result is not None


def test_direction_sanitizer_removes_unknown_english_brand_but_retains_subject():
    result = director.sanitize_visual_direction(
        {"subject": "кофе", "palette": "Acme", "light": "дневной"},
        owner_context="кофе",
    )
    assert result is not None
    assert result["palette"] == ""
    assert result["light"] == "дневной"
