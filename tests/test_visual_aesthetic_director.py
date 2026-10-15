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
