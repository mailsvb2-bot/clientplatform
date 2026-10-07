from __future__ import annotations

from types import SimpleNamespace

from clientplatform.domain.visual_prompt_compiler import (
    compile_visual_prompt,
    semantic_flags_for_request,
)
from clientplatform.domain.visual_scene_contract import (
    VisualSceneContract,
    fallback_scene_contract,
)
from clientplatform.domain.visual_style_intent import VisualStyleIntent
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


def test_yandex_v11_compiles_scene_contract_before_verbose_prompt() -> None:
    request = "ёж, который слушает ресурсное аудио и становится добрым и пушистым"
    contract = VisualSceneContract(
        version=1,
        topology="transformation",
        primary_subject="ёж",
        initial_state=("колючий",),
        actions=("слушает ресурсное аудио",),
        cause="слушает ресурсное аудио",
        transition=("становится",),
        final_state=("добрым", "пушистым"),
        explicit_text=(),
        required_evidence=(
            "same subject identity across stages",
            "visible audio interaction",
        ),
        forbidden=("stage labels or arrows unless explicitly requested",),
    )
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        scene_contract=contract,
        scene_direction="Stage the immutable meaning cinematically.",
        style_intent=VisualStyleIntent(realism="illustrative"),
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
        scene_contract=contract.to_mapping(),
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert len(adapted.prompt) <= 500
    assert adapted.prompt.startswith("ёж, который слушает")
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in adapted.prompt
    assert "три стадии" not in adapted.prompt
    assert "слушает ресурсное аудио" in adapted.prompt
    assert "добрым" in adapted.prompt
    assert "пушистым" in adapted.prompt
    assert "кинематографичная постановка" in adapted.prompt
    assert "BEFORE" not in adapted.prompt
    assert "AFTER" not in adapted.prompt
    assert "ДО →" not in adapted.prompt


def test_yandex_keeps_owner_supplement_before_generic_art_direction() -> None:
    request = "кошка смотрит на дождь за окном"
    contract = VisualSceneContract(
        version=1,
        topology="action",
        primary_subject="кошка",
        initial_state=(),
        actions=("смотрит",),
        cause="",
        transition=(),
        final_state=(),
        explicit_text=(),
        required_evidence=("visible gaze connected to viewed source",),
        forbidden=("unrelated subject",),
    )
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        scene_contract=contract,
        scene_direction=(
            "Stage the immutable meaning cinematically. Owner refinement: "
            "ночной мягкий свет, камера немного ниже уровня глаз. "
            "Apply this only where compatible with the canonical semantic contract; "
            "the contract remains mandatory."
        ),
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
        scene_contract=contract.to_mapping(),
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert len(adapted.prompt) <= 500
    assert "Уточнение пользователя: ночной мягкий свет" in adapted.prompt
    assert "кинематографичная постановка" in adapted.prompt
    assert adapted.prompt.index("Уточнение пользователя") < adapted.prompt.index(
        "кинематографичная постановка"
    )


def test_yandex_scene_contract_is_generic_for_object_replacement() -> None:
    request = "замени старую раковину на новую в той же ванной"
    contract = VisualSceneContract(
        version=1,
        topology="replacement",
        primary_subject="раковину",
        initial_state=("старую раковину",),
        actions=("замени",),
        cause="",
        transition=(),
        final_state=("новую",),
        explicit_text=(),
        required_evidence=("same environment across replacement",),
        forbidden=("unrelated room redesign",),
    )
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        scene_contract=contract,
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
        scene_contract=contract.to_mapping(),
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert len(adapted.prompt) <= 500
    assert "замени старую раковину" in adapted.prompt
    assert "Покажи замену в том же окружении" in adapted.prompt
    assert "физически правдоподобный результат" in adapted.prompt


def test_yandex_adapter_uses_natural_owner_description_without_compiler_meta() -> None:
    adapted = adapt_visual_brief_for_provider(_compiled_brief(), provider="yandexart")

    assert adapted.prompt.startswith(
        "a prickly hedgehog listens to an audio session and becomes gentle"
    )
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in adapted.prompt
    assert "три стадии" not in adapted.prompt
    assert "ДО →" not in adapted.prompt
    assert "ДЕЙСТВИЕ/ПРИЧИНА" not in adapted.prompt
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


def test_yandex_adapter_expands_resource_audio_transformation_into_visual_stages() -> None:
    request = (
        "ёж, который слушает ресурсные аудио трансы "
        "и становится добрым и пушистым"
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

    assert adapted.prompt.startswith(request)
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in adapted.prompt
    assert "три стадии" not in adapted.prompt
    assert "сначала обычный" not in adapted.prompt
    assert "слушает аудио" in adapted.prompt
    assert "наушниках" in adapted.prompt
    assert "не символом волны" in adapted.prompt
    assert "запрошенный результат уже частично проявился" in adapted.prompt
    assert "доброжелательный расслабленный взгляд" in adapted.prompt
    assert "явно пушистая объёмная фактура" in adapted.prompt
    assert "Не своди запрос к готовому статичному финалу" in adapted.prompt
    assert "ДО →" not in adapted.prompt
    assert "Owner request" not in adapted.prompt
    assert "mandatory" not in adapted.prompt.casefold()
    assert len(adapted.prompt) <= 500


def test_yandex_production_contract_keeps_one_scene_for_gradual_change() -> None:
    """The paid path always attaches a fallback scene contract.

    That contract used to replace the owner sentence with the noun "Ёж" and
    tell Alice to draw the same subject three times. A gradual change is one
    scene: the action and the resulting state together.
    """

    request = (
        "Ёж, который слушает ресурсные аудиотрансы "
        "и постепенно становится добрым и пушистым"
    )
    flags = semantic_flags_for_request(request)
    contract = fallback_scene_contract(request=request, semantic_flags=flags)
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        scene_contract=contract,
    )
    adapted = adapt_visual_brief_for_provider(
        CreativeBrief(
            kind="image",
            prompt=compiled.prompt,
            country_code="RU",
            aspect_ratio="4:5",
            negative_prompt=compiled.negative_prompt,
            scene_contract=contract.to_mapping(),
        ),
        provider="yandexart",
    )

    assert "storyboard" not in flags
    assert adapted.prompt.startswith(request)
    assert "ресурсные аудиотрансы" in adapted.prompt
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in adapted.prompt
    assert "наушниках" in adapted.prompt
    assert "запрошенный результат уже частично проявился" in adapted.prompt
    assert "доброжелательный расслабленный взгляд" in adapted.prompt
    assert "явно пушистая объёмная фактура" in adapted.prompt
    assert "Не своди запрос к готовому статичному финалу" in adapted.prompt
    assert "три стадии" not in adapted.prompt
    assert "visible progressive change" not in adapted.prompt
    assert "исходное состояние" not in adapted.prompt
    assert len(adapted.prompt) <= 500
    responses = str(adapted.metadata["yandex_responses_input"])
    assert responses.startswith(request)
    assert "три стадии" not in responses
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in responses

def test_yandex_responses_input_preserves_scene_semantics_without_compiler_meta() -> None:
    request = (
        "ёж, который слушает ресурсные аудио трансы "
        "и становится добрым и пушистым"
    )
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        style_intent=VisualStyleIntent(quick_styles="warm_friendly,illustrative"),
    )
    adapted = adapt_visual_brief_for_provider(
        CreativeBrief(
            kind="image",
            prompt=compiled.prompt,
            country_code="RU",
            aspect_ratio="4:5",
            negative_prompt=compiled.negative_prompt,
        ),
        provider="yandexart",
    )

    responses_input = str(adapted.metadata["yandex_responses_input"])
    assert responses_input.startswith(request)
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in responses_input
    assert "три стадии" not in responses_input
    assert "слушает аудио" in responses_input
    assert "наушниках" in responses_input
    assert "запрошенный результат уже частично проявился" in responses_input
    assert "доброжелательный расслабленный взгляд" in responses_input
    assert "явно пушистая объёмная фактура" in responses_input
    assert "Owner request" not in responses_input
    assert "mandatory" not in responses_input.casefold()
    assert "BEFORE" not in responses_input
    assert "AFTER" not in responses_input
    assert "тёплый" in responses_input or "warm" in responses_input.casefold()
    assert len(responses_input) > len(request)
    assert len(adapted.prompt) <= 500


def test_yandex_preserves_owner_authored_three_stage_transformation_in_auto_and_artistic_modes() -> None:
    request = (
        "ёж во время прослушивания ресурсного аудио постепенно меняется: "
        "сначала он напряжённый, настороженный и очень колючий; "
        "затем, продолжая слушать в заметных наушниках, его выражение становится "
        "спокойнее, поза расслабляется, иголки постепенно смягчаются; "
        "в финальной стадии это тот же узнаваемый ёж, но уже доброжелательный, "
        "расслабленный и заметно пушистый. Трансформация должна визуально читаться "
        "слева направо как непрерывное изменение одного персонажа, а не три разных ежа."
    )
    styles = (
        None,
        VisualStyleIntent(
            quick_styles="warm_friendly,premium,illustrative",
        ),
    )

    for style in styles:
        compiled = compile_visual_prompt(
            request=request,
            kind="image",
            style_intent=style,
        )
        brief = CreativeBrief(
            kind="image",
            prompt=compiled.prompt,
            country_code="RU",
            aspect_ratio="4:5",
            negative_prompt=compiled.negative_prompt,
        )

        adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

        assert len(adapted.prompt) <= 500
        assert adapted.prompt.startswith("ёж во время прослушивания ресурсного")
        assert "Один герой, три стадии без подписей" in adapted.prompt
        assert "напряжённая поза" in adapted.prompt
        assert "настороженный взгляд" in adapted.prompt
        assert "колючая жёсткая фактура" in adapted.prompt
        assert "слушает аудио в заметных наушниках" in adapted.prompt
        assert "спокойный взгляд" in adapted.prompt
        assert "фактура/форма смягчается" in adapted.prompt
        assert "доброжелательный взгляд" in adapted.prompt
        assert "пушистая объёмная фактура" in adapted.prompt
        assert "ДО →" not in adapted.prompt
        assert "ДЕЙСТВИЕ" not in adapted.prompt
        assert "ПРИЧИНА" not in adapted.prompt

        if style is not None:
            assert "тёплый дружелюбный" in adapted.prompt
            assert "премиальный" in adapted.prompt
            assert "художественный" in adapted.prompt


def test_yandex_stage_prompt_keeps_final_state_and_artistic_style_with_brand_context() -> None:
    request = (
        "ёж, который слушает ресурсные аудио трансы "
        "и становится добрым и пушистым"
    )
    brand_context = "Example Wellness: guided resource audio."
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        brand_context=brand_context,
        style_intent=VisualStyleIntent(
            quick_styles="warm_friendly,premium,illustrative",
        ),
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

    assert len(adapted.prompt) <= 500
    assert adapted.prompt.startswith(request)
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in adapted.prompt
    assert "три стадии" not in adapted.prompt
    assert "слушает аудио в заметных наушниках" in adapted.prompt
    assert "доброжелательный расслабленный взгляд" in adapted.prompt
    assert "явно пушистая объёмная фактура" in adapted.prompt
    assert "тёплый дружелюбный" in adapted.prompt
    assert "премиальный" in adapted.prompt
    assert "художественный" in adapted.prompt
    assert "Названия бренда/услуг не печатать без явного запроса" in adapted.prompt


def test_transformation_final_state_does_not_absorb_another_subject_state() -> None:
    request = "злой ёж становится добрым и обнимает грустного друга"
    compiled = compile_visual_prompt(request=request, kind="image")
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert "напряжённый взгляд и жёсткая поза" in adapted.prompt
    assert "доброжелательный расслабленный взгляд" in adapted.prompt
    assert "опущенный взгляд и сдержанная закрытая поза" not in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_from_to_transformation_preserves_explicit_initial_and_final_states() -> None:
    request = "кот меняется из злого в доброго"
    compiled = compile_visual_prompt(request=request, kind="image")
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert "напряжённый взгляд и жёсткая поза" in adapted.prompt
    assert "доброжелательный расслабленный взгляд" in adapted.prompt
    assert "сначала обычный" not in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_transformation_state_scope_stops_before_secondary_subject_without_action() -> None:
    request = "злой ёж становится добрым рядом с грустным другом"
    compiled = compile_visual_prompt(request=request, kind="image")
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert "напряжённый взгляд и жёсткая поза" in adapted.prompt
    assert "доброжелательный расслабленный взгляд" in adapted.prompt
    assert "опущенный взгляд и сдержанная закрытая поза" not in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_unparsed_transformation_does_not_invent_neutral_or_final_state() -> None:
    request = "кот меняется из красного в синего"
    compiled = compile_visual_prompt(request=request, kind="image")
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert "исходное и итоговое состояния бери только из запроса" in adapted.prompt
    assert "сначала обычный" not in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_explicit_before_after_labels_are_not_suppressed_by_provider_cue() -> None:
    request = 'злой ёж становится добрым, коллаж до/после, подпись слева «ДО», справа «ПОСЛЕ»'
    compiled = compile_visual_prompt(request=request, kind="image")
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert adapted.prompt.startswith(request)
    assert "Один герой, три стадии" in adapted.prompt
    assert "Один герой, три стадии без подписей" not in adapted.prompt
    assert "Без читаемого текста" not in adapted.prompt
    assert "Только запрошенный текст; без других надписей" in adapted.prompt
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

    assert "явно слушает аудио" in adapted.prompt
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in adapted.prompt
    assert "три стадии" not in adapted.prompt
    assert "ДО →" not in adapted.prompt
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
        "a hedgehog listens to a guided audio wellness session"
    )
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in adapted.prompt
    assert "три стадии" not in adapted.prompt
    assert "явно слушает аудио" in adapted.prompt
    assert "ДО →" not in adapted.prompt
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

    assert adapted.prompt.startswith(request)
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in adapted.prompt
    assert "три стадии" not in adapted.prompt
    assert "сначала обычный" not in adapted.prompt
    assert "слушает аудио" in adapted.prompt
    assert "не символом волны" in adapted.prompt
    assert "доброжелательный расслабленный взгляд" in adapted.prompt
    assert "явно пушистая объёмная фактура" in adapted.prompt
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
    assert "Не печатай названия бренда/услуг/методов" in adapted.prompt
    assert "явного запроса на это" in adapted.prompt
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
    assert adapted.prompt.startswith("ёж слушает ресурсное аудио")
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in adapted.prompt
    assert "три стадии" not in adapted.prompt
    assert "слушает аудио" in adapted.prompt
    assert "доброжелательный расслабленный взгляд" in adapted.prompt
    assert "явно пушистая объёмная фактура" in adapted.prompt
    assert "Без водяных знаков" in adapted.prompt
    assert "Без выдуманных логотипов" in adapted.prompt
    assert "полностью в кадре" in adapted.prompt
    assert "Без читаемого текста" in adapted.prompt
    assert "Не печатай названия бренда/услуг/методов" in adapted.prompt
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
    assert result.provider_payload["prompt_adapter_version"] == 14
    assert "Owner request" not in captured["brief"].prompt
    assert "hedgehog listens to an audio session" in captured["brief"].prompt


def test_engine_respects_explicit_text_from_compiled_contract(monkeypatch) -> None:
    captured = {}
    request = (
        'злой ёж становится добрым, коллаж до/после, '
        'подпись слева «ДО», справа «ПОСЛЕ»'
    )
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        brand_context="Example Wellness: guided resource audio.",
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
        brand_context="Example Wellness: guided resource audio.",
    )

    class FakeProvider:
        def configured(self, kind):
            return kind == "image"

        def submit(self, provider_brief):
            captured["brief"] = provider_brief
            return CreativeJob(
                provider="yandexart",
                kind="image",
                status="succeeded",
                external_id="job-explicit-text",
            )

    monkeypatch.setattr(engine, "provider_order", lambda *_args, **_kwargs: ("yandexart",))
    monkeypatch.setattr(engine, "build_provider", lambda _name: FakeProvider())

    result = engine.VisualCreativeEngine(enabled=True).submit(brief)

    assert result.status == "succeeded"
    prompt = captured["brief"].prompt
    assert prompt.startswith(request)
    assert "Один герой, три стадии" in prompt
    assert "Один герой, три стадии без подписей" not in prompt
    assert "Без читаемого текста" not in prompt
    assert "Только запрошенный текст; без других надписей" in prompt
    assert "leave clean negative space" not in prompt
    assert "Названия бренда/услуг не печатать без явного запроса" in prompt
    assert len(prompt) <= 500


def test_engine_does_not_reappend_raw_brand_direction_to_compiled_prompt(monkeypatch) -> None:
    captured = {}
    brand_context = "Example Wellness: guided resource audio."
    compiled = compile_visual_prompt(
        request="уютная иллюстрация ежа в наушниках",
        kind="image",
        brand_context=brand_context,
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="DE",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
        brand_context=brand_context,
    )

    class FakeProvider:
        def configured(self, kind):
            return kind == "image"

        def submit(self, provider_brief):
            captured["brief"] = provider_brief
            return CreativeJob(
                provider="openai",
                kind="image",
                status="succeeded",
                external_id="job-openai-compiled",
            )

    monkeypatch.setattr(engine, "provider_order", lambda *_args, **_kwargs: ("openai",))
    monkeypatch.setattr(engine, "build_provider", lambda _name: FakeProvider())

    result = engine.VisualCreativeEngine(enabled=True).submit(brief)

    assert result.status == "succeeded"
    prompt = captured["brief"].prompt
    assert "Brand direction:" not in prompt
    assert "Names from business grounding are semantic context only" in prompt
    assert "Production constraints:" in prompt
    assert "No readable text, letters, captions or UI in the generated pixels." in prompt
    assert "leave clean negative space" not in prompt


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
    assert result.provider_payload["prompt_adapter_version"] == 14
    prompt = captured["brief"].prompt
    assert prompt.startswith(
        "a prickly hedgehog listens to an audio session and becomes gentle"
    )
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in prompt
    assert "три стадии" not in prompt
    assert "ДО →" not in prompt
    assert "ДЕЙСТВИЕ/ПРИЧИНА" not in prompt
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
        "a prickly hedgehog listens to an audio session and becomes gentle"
    )
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in adapted.prompt
    assert "три стадии" not in adapted.prompt
    assert "ДО →" not in adapted.prompt
    assert "Example brand context" not in adapted.prompt
    assert "не печатать" in adapted.prompt
    assert len(adapted.prompt) <= 500
    assert "Owner request" not in adapted.prompt

def test_yandex_scene_contract_preserves_exact_requested_visible_text() -> None:
    request = "красная чашка с надписью СКИДКА"
    contract = VisualSceneContract(
        version=1,
        topology="static",
        primary_subject="красная чашка",
        initial_state=(),
        actions=(),
        cause="",
        transition=(),
        final_state=(),
        explicit_text=("СКИДКА",),
        required_evidence=("requested visible text present exactly: СКИДКА",),
        forbidden=("readable text other than owner-requested wording",),
    )
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        scene_contract=contract,
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="1:1",
        negative_prompt=compiled.negative_prompt,
        scene_contract=contract.to_mapping(),
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    assert len(adapted.prompt) <= 500
    assert adapted.prompt.startswith("красная чашка")
    assert "Точный запрошенный текст в кадре" in adapted.prompt
    assert "СКИДКА" in adapted.prompt


def test_yandex_responses_receives_full_selected_art_direction() -> None:
    request = "ёж слушает ресурсное аудио и постепенно становится добрым и пушистым"
    contract = VisualSceneContract(
        version=1,
        topology="transformation",
        primary_subject="ёж",
        initial_state=("колючий",),
        actions=("слушает ресурсное аудио",),
        cause="слушает ресурсное аудио",
        transition=("становится",),
        final_state=("добрым", "пушистым"),
        explicit_text=(),
        required_evidence=("visible audio interaction",),
        forbidden=("unrelated subject",),
    )
    detailed_direction = (
        "Use a cinematic medium-wide composition with the same hedgehog repeated "
        "across three visually connected moments. Start with tense posture and sparse "
        "quills, place visible headphones and a small audio player in the causal middle "
        "stage, then finish with relaxed eyes, open posture and visibly fuller soft fur. "
        "Use warm practical light that gradually increases from left to right, keep the "
        "background coherent across all stages, avoid decorative clutter, and make the "
        "cause-and-effect readable without captions, arrows or symbolic wave graphics."
    )
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        scene_contract=contract,
        scene_direction=detailed_direction,
    )
    brief = CreativeBrief(
        kind="image",
        prompt=compiled.prompt,
        country_code="RU",
        aspect_ratio="4:5",
        negative_prompt=compiled.negative_prompt,
        scene_contract=contract.to_mapping(),
    )

    adapted = adapt_visual_brief_for_provider(brief, provider="yandexart")

    responses_input = str(adapted.metadata.get("yandex_responses_input") or "")
    assert len(adapted.prompt) <= 500
    assert "Режиссёрская постановка — соблюсти полностью:" in responses_input
    assert "same hedgehog repeated across three visually connected moments" in responses_input
    assert "visible headphones and a small audio player" in responses_input
    assert "warm practical light that gradually increases from left to right" in responses_input
    assert "avoid decorative clutter" in responses_input
    assert "symbolic wave graphics" in responses_input


def test_generic_state_transformation_is_not_hardcoded_to_any_subject() -> None:
    request = (
        "зелёный лист постепенно становится сухим и ломким, "
        "края скручиваются"
    )
    compiled = compile_visual_prompt(request=request, kind="image")
    adapted = adapt_visual_brief_for_provider(
        CreativeBrief(
            kind="image",
            prompt=compiled.prompt,
            country_code="RU",
            aspect_ratio="4:5",
            negative_prompt=compiled.negative_prompt,
        ),
        provider="yandexart",
    )

    assert "transformation" in compiled.semantic_flags
    assert adapted.prompt.startswith(request)
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in adapted.prompt
    assert "сам переход виден на объекте, материале, фактуре, форме, позе" in adapted.prompt
    assert "Не своди запрос к готовому статичному финалу" in adapted.prompt
    assert "ёж" not in adapted.prompt.casefold()
    assert "мех" not in adapted.prompt.casefold()
    assert "наушник" not in adapted.prompt.casefold()
    assert len(adapted.prompt) <= 500


def test_presentation_edit_changes_rendering_not_subject_physics() -> None:
    request = (
        "Сделай городскую улицу в стиле акварели, "
        "сохрани людей, здания и композицию"
    )
    flags = semantic_flags_for_request(request)
    contract = fallback_scene_contract(request=request, semantic_flags=flags)
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        scene_contract=contract,
    )
    adapted = adapt_visual_brief_for_provider(
        CreativeBrief(
            kind="image",
            prompt=compiled.prompt,
            country_code="RU",
            aspect_ratio="4:5",
            negative_prompt=compiled.negative_prompt,
            scene_contract=contract.to_mapping(),
        ),
        provider="yandexart",
    )

    assert "presentation_change" in flags
    assert "transformation" not in flags
    assert contract.topology == "static"
    assert adapted.prompt.startswith(request)
    assert "Сохрани объект, сюжет, геометрию и действия" in adapted.prompt
    assert "измени только запрошенную визуальную подачу" in adapted.prompt
    assert "Без физической мутации" in adapted.prompt
    assert "главный объект в процессе изменения" not in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_presentation_transition_keeps_scene_identity_without_physical_mutation() -> None:
    request = (
        "Изображение городской улицы постепенно становится акварельным, "
        "архитектура и люди остаются теми же"
    )
    flags = semantic_flags_for_request(request)
    contract = fallback_scene_contract(request=request, semantic_flags=flags)
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        scene_contract=contract,
    )
    adapted = adapt_visual_brief_for_provider(
        CreativeBrief(
            kind="image",
            prompt=compiled.prompt,
            country_code="RU",
            aspect_ratio="4:5",
            negative_prompt=compiled.negative_prompt,
            scene_contract=contract.to_mapping(),
        ),
        provider="yandexart",
    )

    assert "presentation_change" in flags
    assert "presentation_transition" in flags
    assert "transformation" in flags
    assert contract.topology == "static"
    assert adapted.prompt.startswith(request)
    assert "Одна сцена, тот же объект и тот же сюжет" in adapted.prompt
    assert "меняется только визуальная подача" in adapted.prompt
    assert "не превращай его в физическую мутацию объекта" in adapted.prompt
    assert "главный объект в процессе изменения" not in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_subject_change_and_selected_rendering_style_are_independent_axes() -> None:
    request = "каменная статуя постепенно становится мягкой и гибкой"
    compiled = compile_visual_prompt(
        request=request,
        kind="image",
        style_intent=VisualStyleIntent(
            realism="illustrative",
            quick_styles="warm_friendly",
        ),
    )
    adapted = adapt_visual_brief_for_provider(
        CreativeBrief(
            kind="image",
            prompt=compiled.prompt,
            country_code="RU",
            aspect_ratio="4:5",
            negative_prompt=compiled.negative_prompt,
        ),
        provider="yandexart",
    )

    assert "transformation" in compiled.semantic_flags
    assert "presentation_change" not in compiled.semantic_flags
    assert "Одна сцена, один и тот же главный объект в процессе изменения" in adapted.prompt
    assert "мяг" in adapted.prompt.casefold()
    assert "художественный" in adapted.prompt or "тёплый" in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_arbitrary_owner_style_wording_survives_yandex_compaction() -> None:
    request = (
        "Большая городская площадь после дождя, люди идут вдоль старых фасадов, "
        "отражения в лужах, много мелких архитектурных деталей, мягкая перспектива, "
        "естественная повседневная сцена без рекламных элементов, "
        "в стиле современной линогравюры"
    )
    compiled = compile_visual_prompt(request=request, kind="image")
    adapted = adapt_visual_brief_for_provider(
        CreativeBrief(
            kind="image",
            prompt=compiled.prompt,
            country_code="RU",
            aspect_ratio="4:5",
            negative_prompt=compiled.negative_prompt,
        ),
        provider="yandexart",
    )

    assert "Обязательный стиль пользователя" in adapted.prompt
    assert "в стиле современной линогравюры" in adapted.prompt
    assert "главный объект в процессе изменения" not in adapted.prompt
    assert len(adapted.prompt) <= 500


def test_non_animal_fluffy_transformation_does_not_invent_fur_or_needles() -> None:
    request = "старый плед постепенно становится мягким и пушистым"
    compiled = compile_visual_prompt(request=request, kind="image")
    adapted = adapt_visual_brief_for_provider(
        CreativeBrief(
            kind="image",
            prompt=compiled.prompt,
            country_code="RU",
            aspect_ratio="4:5",
            negative_prompt=compiled.negative_prompt,
        ),
        provider="yandexart",
    )

    assert adapted.prompt.startswith(request)
    assert "пушистая объёмная фактура" in adapted.prompt
    assert "фактура визуально мягче" in adapted.prompt
    assert "мех" not in adapted.prompt.casefold()
    assert "шерст" not in adapted.prompt.casefold()
    assert "игл" not in adapted.prompt.casefold()
    assert len(adapted.prompt) <= 500


def test_single_scene_transformation_keeps_explicit_initial_evidence() -> None:
    request = "злой кот постепенно становится добрым"
    compiled = compile_visual_prompt(request=request, kind="image")
    adapted = adapt_visual_brief_for_provider(
        CreativeBrief(
            kind="image",
            prompt=compiled.prompt,
            country_code="RU",
            aspect_ratio="4:5",
            negative_prompt=compiled.negative_prompt,
        ),
        provider="yandexart",
    )

    assert "исходные признаки ещё частично видны" in adapted.prompt
    assert "напряжённый взгляд и жёсткая поза" in adapted.prompt
    assert "доброжелательный расслабленный взгляд" in adapted.prompt
    assert "часть исходных признаков ещё видна" not in adapted.prompt
    assert len(adapted.prompt) <= 500
