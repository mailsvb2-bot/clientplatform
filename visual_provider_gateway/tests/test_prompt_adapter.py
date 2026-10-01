from __future__ import annotations

from types import SimpleNamespace

from visual_provider_gateway import engine
from visual_provider_gateway.models import CreativeBrief, CreativeJob
from visual_provider_gateway.prompt_adapter import adapt_visual_brief_for_provider


def _compiled_brief(*, kind: str = "image") -> CreativeBrief:
    prompt = "\n".join(
        [
            "1. Create one polished visual.",
            "2. Semantic fidelity to the owner's idea is the primary objective.",
            '3. Owner request, preserve its meaning exactly: "a prickly hedgehog listens to an audio session and becomes gentle".',
            "4. Every explicit subject, action, relationship and state change is mandatory.",
            "5. If the subject is listening, make the listening unmistakable through visible audio interaction.",
            "6. The transformation is mandatory visual evidence. Show the initial and final states of the same subject.",
            "7. " + ("background filler " * 80),
            "8. Style choices may shape presentation but must never remove mandatory actions.",
            "9. Use a warm color temperature. The emotional tone should feel calm and gentle.",
            "10. Business grounding: audio wellness session.",
        ]
    )
    return CreativeBrief(
        kind=kind,
        prompt=prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt="missing requested action",
    )


def test_yandex_adapter_keeps_frozen_semantics_and_strengthens_action_priority() -> None:
    adapted = adapt_visual_brief_for_provider(_compiled_brief(), provider="yandexart")

    assert "Render the owner's requested scene faithfully" in adapted.prompt
    assert "hedgehog listens to an audio session" in adapted.prompt
    assert adapted.negative_prompt == "missing requested action"
    assert len(adapted.prompt) <= 500


def test_yandex_motion_adapter_marks_keyframe_constraint_without_new_story() -> None:
    adapted = adapt_visual_brief_for_provider(
        _compiled_brief(kind="video"),
        provider="yandexart_motion",
    )

    assert "Create a keyframe" in adapted.prompt
    assert "hedgehog listens to an audio session" in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_runway_adapter_preserves_semantics_and_style_inside_hard_prompt_limit() -> None:
    adapted = adapt_visual_brief_for_provider(
        _compiled_brief(kind="video"),
        provider="runway",
    )

    assert len(adapted.prompt) <= 1000
    assert "hedgehog listens to an audio session" in adapted.prompt
    assert "listening unmistakable" in adapted.prompt
    assert "transformation is mandatory" in adapted.prompt.casefold()
    assert "warm color temperature" in adapted.prompt


def test_openai_adapter_does_not_rewrite_provider_neutral_compiled_prompt() -> None:
    brief = _compiled_brief()
    adapted = adapt_visual_brief_for_provider(brief, provider="openai")

    assert adapted == brief.normalized()


def test_engine_applies_adapter_only_after_provider_selection(monkeypatch) -> None:
    captured = {}

    class FakeProvider:
        def configured(self, kind):
            return kind == "image"

        def submit(self, brief):
            captured["brief"] = brief
            return CreativeJob(
                provider="yandexart",
                kind="image",
                status="succeeded",
                external_id="job-1",
            )

    monkeypatch.setattr(engine, "provider_order", lambda *_args, **_kwargs: ("yandexart",))
    monkeypatch.setattr(engine, "build_provider", lambda _name: FakeProvider())

    result = engine.VisualCreativeEngine(enabled=True).submit(_compiled_brief())

    assert result.status == "succeeded"
    assert result.provider_payload["prompt_adapter_version"] == 2
    assert "Render the owner's requested scene faithfully" in captured["brief"].prompt
    assert "hedgehog listens to an audio session" in captured["brief"].prompt
