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


def test_director_drops_invented_discounts_and_brand():
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


def test_director_degrades_without_provider():
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
