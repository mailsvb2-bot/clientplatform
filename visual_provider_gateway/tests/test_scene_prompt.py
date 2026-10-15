from __future__ import annotations

from visual_provider_gateway.scene_prompt import (
    VisualDirection,
    build_direction_prompt,
    direction_from_brief_metadata,
    fallback_visual_direction,
    negative_prompt_for,
)


def test_direction_is_positive_bounded_and_russian():
    direction = VisualDirection(
        subject="белая чашка кофе",
        setting="утреннее кафе",
        light="мягкий дневной",
        style="фотореализм",
        palette="бежевые цвета",
    )
    prompt = build_direction_prompt(direction)
    assert prompt.startswith("белая чашка кофе")
    assert "Свет:" in prompt
    assert "без текста" not in prompt
    assert len(prompt) <= 350


def test_fallback_preserves_owner_subject():
    direction = fallback_visual_direction('Owner request, preserve its meaning exactly: "кошка на диване"')
    assert direction is not None
    assert direction.subject == "кошка на диване"


def test_negative_prompt_does_not_block_requested_text():
    assert "text" in negative_prompt_for(explicit_text_allowed=False)
    assert "text" not in negative_prompt_for(explicit_text_allowed=True)


def test_metadata_direction_rejects_non_dictionary_and_unknown_keys():
    assert direction_from_brief_metadata({"visual_direction": "bad"}) is None
    assert direction_from_brief_metadata({"visual_direction": {"subject": "кофе", "price": "200"}}) is None


def test_service_sanitizes_invented_claims_before_provider():
    from visual_provider_gateway.service import VisualGatewayService

    payload = {"prompt": "чашка кофе", "brand_context": ""}
    approved = VisualGatewayService._safe_visual_direction(
        {"subject": "чашка кофе", "light": "мягкий свет", "action": "скидка 90%"},
        payload,
    )
    assert approved is not None
    assert approved["subject"] == "чашка кофе"
    assert approved["light"] == "мягкий свет"
    assert approved["action"] == ""
