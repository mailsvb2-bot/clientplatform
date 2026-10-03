from __future__ import annotations

from types import SimpleNamespace

from clientplatform.domain.visual_prompt_compiler import compile_visual_prompt
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
    prompt += (
        "\n\nProduction constraints: No watermarks. "
        "Do not invent brand logos or certifications. "
        "Keep important subjects away from the outer 8 percent safe-area edges. "
        "No readable text, letters, captions or UI in the generated pixels."
    )
    return CreativeBrief(
        kind=kind,
        prompt=prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=(
            "missing requested action; watermark; invented logo; "
            "cropped important subject; readable advertising text baked into image"
        ),
    )


def test_yandex_adapter_uses_natural_owner_description_without_compiler_meta() -> None:
    adapted = adapt_visual_brief_for_provider(_compiled_brief(), provider="yandexart")

    assert adapted.prompt.startswith(
        "Один субъект: до → действие/причина → после."
    )
    assert "a prickly hedgehog listens to an audio session and becomes gentle" in adapted.prompt
    assert "Owner request" not in adapted.prompt
    assert "Render the owner's requested scene faithfully" not in adapted.prompt
    assert "mandatory" not in adapted.prompt.casefold()
    assert "Production constraints" not in adapted.prompt
    assert "Без водяных знаков" in adapted.prompt
    assert "Без выдуманных логотипов" in adapted.prompt
    assert "полностью в кадре" in adapted.prompt
    assert "Без читаемого текста" in adapted.prompt
    assert "тёплая гамма" in adapted.prompt
    assert "missing requested action" in adapted.negative_prompt
    assert len(adapted.prompt) <= 500


def test_yandex_adapter_preserves_exact_owner_listening_and_transformation() -> None:
    removed_product_method = "метро" + "терапию"
    request = (
        "ёж, который слушает "
        + removed_product_method
        + " и становится добрым и пушистым"
    )
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert adapted.prompt.startswith(
        "Один субъект: до → действие/причина → после."
    )
    assert request in adapted.prompt
    assert removed_product_method in adapted.prompt
    assert "Слушает аудио" in adapted.prompt
    assert "наушники" in adapted.prompt
    assert "Три фазы одного субъекта" in adapted.prompt
    assert "действие/причина" in adapted.prompt
    assert "не один финальный портрет" in adapted.prompt
    assert "становится добрым и пушистым" in adapted.prompt
    assert "Owner request" not in adapted.prompt
    assert "mandatory" not in adapted.prompt.casefold()
    assert len(adapted.prompt) <= 500


def test_yandex_adapter_does_not_turn_business_name_into_image_text() -> None:
    removed_product_method = "метро" + "терапию"
    request = (
        "ёж, который слушает "
        + removed_product_method
        + " и становится добрым и пушистым"
    )
    brand_name = "Metro" + "therapy"
    brand_context = f"Brand name: {brand_name}. Product: guided audio."
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        brand_context=brand_context,
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
        brand_context=brand_context,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert "Контекст бренда:" not in adapted.prompt
    assert "Brand name:" not in adapted.prompt
    assert "не печатать" in adapted.prompt
    assert "логотип" in adapted.prompt
    assert "Без читаемого текста" in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_yandex_adapter_compiles_sink_replacement_as_complete_installation() -> None:
    compiled = compile_visual_prompt(
        request="Замена раковины",
        kind="image",
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert adapted.prompt.startswith(
        "Покажи именно событие замены, сохрани то же окружение."
    )
    assert "Замена раковины" in adapted.prompt
    assert "монтаж нового объекта" in adapted.prompt or "до/после" in adapted.prompt
    assert "не одиночный предмет" in adapted.prompt
    assert "физически правдоподобен" in adapted.prompt
    assert "управление" in adapted.prompt
    assert "подключения" in adapted.prompt
    assert "Без читаемого текста" in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_yandex_adapter_keeps_generic_action_and_autonomous_story_scene() -> None:
    compiled = compile_visual_prompt(
        request="мальчик бежит за автобусом по мокрой улице",
        kind="image",
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert adapted.prompt.startswith("мальчик бежит за автобусом")
    assert "действие явно видно" in adapted.prompt
    assert "Сюжетная сцена" in adapted.prompt
    assert "статичный портрет" in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_yandex_adapter_preserves_multiple_selected_styles_with_semantics() -> None:
    prompt = "\n".join(
        [
            "1. Create one polished visual.",
            "2. Semantic fidelity to the owner's idea is the primary objective.",
            '3. Owner request, preserve its meaning exactly: "ёж слушает аудиосессию и становится добрым".',
            "4. If the subject is listening, make the listening unmistakable through visible audio interaction.",
            "5. The transformation is mandatory visual evidence. Show the initial and final states of the same subject.",
            "6. Visible-state translation: turn abstract qualities into concrete visual evidence.",
            "7. Style choices may shape presentation but must never remove mandatory actions.",
            "8. Blend in a warm, welcoming and approachable visual character.",
            "9. Blend in a refined premium feel with restrained, polished visual cues.",
            "10. Blend in an artistic, crafted visual treatment rather than a generic stock look.",
            "11. Use credible natural details.",
        ]
    )
    brief = CreativeBrief(
        kind="image",
        prompt=prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=(
            "watermark; invented logo; cropped important subject; "
            "readable advertising text baked into image"
        ),
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert "Слушает аудио" in adapted.prompt
    assert "исходное состояние" in adapted.prompt
    assert "Стиль:" in adapted.prompt
    assert "тёплый дружелюбный" in adapted.prompt
    assert "премиальный" in adapted.prompt
    assert "художественный" in adapted.prompt
    assert "Без водяных знаков" in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_yandex_motion_adapter_marks_keyframe_constraint_without_new_story() -> None:
    adapted = adapt_visual_brief_for_provider(
        _compiled_brief(kind="video"),
        provider="yandexart_motion",
    )

    assert adapted.prompt.startswith("Ключевой кадр для короткого вертикального видео:")
    assert "hedgehog listens to an audio session" in adapted.prompt
    assert "Owner request" not in adapted.prompt
    assert "mandatory" not in adapted.prompt.casefold()
    assert "Production constraints" not in adapted.prompt
    assert "Без водяных знаков" in adapted.prompt
    assert "Без выдуманных логотипов" in adapted.prompt
    assert "полностью в кадре" in adapted.prompt
    assert "Без читаемого текста" in adapted.prompt
    assert len(adapted.prompt) <= 500


def _long_owner_brief(*, kind: str) -> CreativeBrief:
    brief = _compiled_brief(kind=kind)
    long_request = " ".join(
        [
            "a hedgehog listens to a guided audio wellness session in a calm room"
            for _ in range(18)
        ]
    )
    prompt = brief.prompt.replace(
        "a prickly hedgehog listens to an audio session and becomes gentle",
        long_request,
    )
    return CreativeBrief(
        kind=brief.kind,
        prompt=prompt,
        country_code=brief.country_code,
        aspect_ratio=brief.aspect_ratio,
        duration_seconds=brief.duration_seconds,
        negative_prompt=brief.negative_prompt,
        brand_context=brief.brand_context,
    )


def test_yandex_adapter_keeps_all_safety_clauses_for_long_owner_request() -> None:
    adapted = adapt_visual_brief_for_provider(
        _long_owner_brief(kind="image"),
        provider="yandexart",
    )

    assert len(adapted.prompt) <= 500
    assert adapted.prompt.startswith(
        "Один субъект: до → действие/причина → после."
    )
    assert "a hedgehog listens to a guided audio wellness session" in adapted.prompt
    assert "Слушает аудио" in adapted.prompt
    assert "исходное состояние" in adapted.prompt
    assert "Без водяных знаков" in adapted.prompt
    assert "Без выдуманных логотипов" in adapted.prompt
    assert "полностью в кадре" in adapted.prompt
    assert "Без читаемого текста" in adapted.prompt
    assert "Owner request" not in adapted.prompt


def test_yandex_motion_adapter_keeps_all_safety_clauses_for_long_owner_request() -> None:
    adapted = adapt_visual_brief_for_provider(
        _long_owner_brief(kind="video"),
        provider="yandexart_motion",
    )

    assert len(adapted.prompt) <= 500
    assert adapted.prompt.startswith("Ключевой кадр для короткого вертикального видео:")
    assert "Без водяных знаков" in adapted.prompt
    assert "Без выдуманных логотипов" in adapted.prompt
    assert "полностью в кадре" in adapted.prompt
    assert "Без читаемого текста" in adapted.prompt
    assert "Owner request" not in adapted.prompt


def test_gigachat_adapter_preserves_listening_transformation_without_compiler_meta() -> None:
    removed_product_method = "метро" + "терапию"
    request = (
        "ёж, который слушает "
        + removed_product_method
        + " и становится добрым и пушистым"
    )
    compiled = compile_visual_prompt(request=request, kind="image")
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="gigachat")

    assert adapted.prompt.startswith("Один субъект: до → действие/причина → после.")
    assert request in adapted.prompt
    assert "Слушает аудио" in adapted.prompt
    assert "наушники" in adapted.prompt
    assert "Три фазы одного субъекта" in adapted.prompt
    assert "не один финальный портрет" in adapted.prompt
    assert "Owner request" not in adapted.prompt
    assert "mandatory" not in adapted.prompt.casefold()
    assert len(adapted.prompt) <= 1800


def test_gigachat_adapter_keeps_sink_replacement_physical_and_contextual() -> None:
    compiled = compile_visual_prompt(request="Замена раковины", kind="image")
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="gigachat")

    assert adapted.prompt.startswith(
        "Покажи именно событие замены, сохрани то же окружение."
    )
    assert "Замена раковины" in adapted.prompt
    assert "монтаж нового объекта" in adapted.prompt or "до/после" in adapted.prompt
    assert "не одиночный предмет" in adapted.prompt
    assert "физически правдоподобен" in adapted.prompt
    assert "управление" in adapted.prompt
    assert "подключения" in adapted.prompt
    assert "Без читаемого текста" in adapted.prompt
    assert len(adapted.prompt) <= 1800


def test_gigachat_adapter_does_not_leak_raw_business_context_into_pixels() -> None:
    brand_name = "Metro" + "therapy"
    brand_context = f"Brand name: {brand_name}. Product: guided audio."
    compiled = compile_visual_prompt(
        request="ёж слушает ресурсное аудио и становится добрым и пушистым",
        kind="image",
        brand_context=brand_context,
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
        brand_context=brand_context,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="gigachat")

    assert "Business grounding" not in adapted.prompt
    assert "Brand name:" not in adapted.prompt
    assert brand_name not in adapted.prompt
    assert "Названия бренда, услуг и методов не печатать в кадре." in adapted.prompt
    assert "Без читаемого текста" in adapted.prompt
    assert len(adapted.prompt) <= 1800


def test_gigachat_adapter_preserves_anti_claim_policy_when_text_is_requested() -> None:
    compiled = compile_visual_prompt(
        request='афиша с надписью "Открытая встреча" для психологической практики',
        kind="image",
        purpose="advertising",
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="gigachat")

    assert "Не выдумывай награды" in adapted.prompt
    assert "отзывы" in adapted.prompt
    assert "статистику" in adapted.prompt
    assert "гарантии" in adapted.prompt
    assert "срочность" in adapted.prompt
    assert "Без читаемого текста" not in adapted.prompt
    assert "Owner request" not in adapted.prompt
    assert len(adapted.prompt) <= 1800


def test_gigachat_adapter_reserves_safety_for_near_limit_owner_request() -> None:
    long_tail = " очень подробно описанная спокойная сцена" * 28
    request = (
        "ёж слушает ресурсное аудио и становится добрым и пушистым"
        + long_tail
    )
    request = request[:1490]
    brand_context = "Brand name: Example Wellness. Product: guided audio."
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        brand_context=brand_context,
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
        brand_context=brand_context,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="gigachat")

    assert len(adapted.prompt) <= 1800
    assert adapted.prompt.startswith("Один субъект: до → действие/причина → после.")
    assert "Слушает аудио" in adapted.prompt
    assert "Три фазы одного субъекта" in adapted.prompt
    assert "Без водяных знаков" in adapted.prompt
    assert "Без выдуманных логотипов" in adapted.prompt
    assert "полностью в кадре" in adapted.prompt
    assert "Без читаемого текста" in adapted.prompt
    assert "Названия бренда, услуг и методов не печатать в кадре." in adapted.prompt
    assert "Не выдумывай награды" in adapted.prompt
    assert "Brand name:" not in adapted.prompt


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
    assert "Без водяных знаков" in adapted.prompt
    assert "Без выдуманных логотипов" in adapted.prompt


def test_openai_adapter_does_not_rewrite_provider_neutral_compiled_prompt() -> None:
    brief = _compiled_brief()
    adapted = adapt_visual_brief_for_provider(brief, provider="openai")

    assert adapted == brief.normalized()


def test_engine_motion_adapter_never_forwards_compiler_control_language(monkeypatch) -> None:
    captured = {}

    class FakeProvider:
        def configured(self, kind):
            return kind == "video"

        def submit(self, brief):
            captured["brief"] = brief
            return CreativeJob(
                provider="yandexart_motion",
                kind="video",
                status="succeeded",
                external_id="job-video-1",
            )

    monkeypatch.setattr(
        engine,
        "provider_order",
        lambda *_args, **_kwargs: ("yandexart_motion",),
    )
    monkeypatch.setattr(engine, "build_provider", lambda _name: FakeProvider())

    result = engine.VisualCreativeEngine(enabled=True).submit(
        _compiled_brief(kind="video")
    )

    assert result.status == "succeeded"
    prompt = captured["brief"].prompt
    assert prompt.startswith("Ключевой кадр для короткого вертикального видео:")
    assert "Owner request" not in prompt
    assert "mandatory" not in prompt.casefold()
    assert "Production constraints" not in prompt
    assert "Без водяных знаков" in prompt
    assert "Без читаемого текста" in prompt
    assert len(prompt) <= 500


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
    assert result.provider_payload["prompt_adapter_version"] == 7
    assert "Owner request" not in captured["brief"].prompt
    assert "hedgehog listens to an audio session" in captured["brief"].prompt


def test_engine_applies_meaning_adapter_to_gigachat_fallback(monkeypatch) -> None:
    captured = {}

    class FakeProvider:
        def configured(self, kind):
            return kind == "image"

        def submit(self, brief):
            captured["brief"] = brief
            return CreativeJob(
                provider="gigachat",
                kind="image",
                status="succeeded",
                external_id="job-giga-1",
            )

    monkeypatch.setattr(engine, "provider_order", lambda *_args, **_kwargs: ("gigachat",))
    monkeypatch.setattr(engine, "build_provider", lambda _name: FakeProvider())

    result = engine.VisualCreativeEngine(enabled=True).submit(_compiled_brief())

    assert result.status == "succeeded"
    assert result.provider_payload["prompt_adapter_version"] == 7
    prompt = captured["brief"].prompt
    assert prompt.startswith("Один субъект: до → действие/причина → после.")
    assert "hedgehog listens to an audio session" in prompt
    assert "Owner request" not in prompt
    assert "mandatory" not in prompt.casefold()


def test_yandex_adapter_keeps_legacy_direct_prompt_natural() -> None:
    brief = CreativeBrief(
        kind="image",
        prompt="Красный круг на белом фоне, минималистичная иллюстрация",
        country_code="RU",
        aspect_ratio="4:5",
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert adapted.prompt == brief.prompt
    assert len(adapted.prompt) <= 500


def test_yandex_adapter_prioritizes_owner_request_before_style_and_brand_context() -> None:
    brief = _compiled_brief()
    brief = CreativeBrief(
        kind=brief.kind,
        prompt=brief.prompt,
        country_code=brief.country_code,
        aspect_ratio=brief.aspect_ratio,
        negative_prompt=brief.negative_prompt,
        brand_context="Example brand context " * 40,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert adapted.prompt.startswith(
        "Один субъект: до → действие/причина → после."
    )
    assert "a prickly hedgehog listens to an audio session and becomes gentle" in adapted.prompt
    assert "Example brand context" not in adapted.prompt
    assert "не печатать" in adapted.prompt
    assert len(adapted.prompt) <= 500
    assert "Owner request" not in adapted.prompt
