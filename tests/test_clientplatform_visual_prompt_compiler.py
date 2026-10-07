from __future__ import annotations

import json
import unittest

from clientplatform.application import visual_creatives
from clientplatform.domain.visual_prompt_compiler import (
    VisualSemanticQAContract,
    build_visual_semantic_qa_contract,
    compile_visual_prompt,
)


class VisualPromptCompilerTests(unittest.TestCase):
    def test_short_owner_request_becomes_explicit_transformation_scene(self) -> None:
        request = (
            "колючий ёж, который слушает аудиосессию «Тишина» "
            "и превращается в доброго и мягкого"
        )
        compiled = compile_visual_prompt(
            request=request,
            kind="image",
            brand_context="Brand name: Тишина. Tone: calm, human.",
        )

        self.assertEqual(
            compiled.semantic_flags,
            ("transformation", "listening", "visible_state", "storyboard"),
        )
        self.assertIn(request, compiled.prompt)
        self.assertIn("listening unmistakable", compiled.prompt)
        self.assertIn("headphones", compiled.prompt)
        self.assertIn("transformation is mandatory visual evidence", compiled.prompt)
        self.assertIn("compact visual storyboard", compiled.prompt)
        self.assertIn("Transformation stage detail", compiled.prompt)
        self.assertNotIn("Show the subject once", compiled.prompt)
        self.assertNotIn("Do not tile duplicate portraits", compiled.prompt)
        self.assertIn("Brand name: Тишина", compiled.prompt)
        self.assertIn("unfamiliar names", compiled.prompt)
        self.assertLess(
            compiled.prompt.index("listening unmistakable"),
            compiled.prompt.index("Business grounding"),
        )
        self.assertIn("generic isolated portrait", compiled.negative_prompt)
        self.assertIn(
            "single-state image with no visible transformation",
            compiled.negative_prompt,
        )
        self.assertNotIn(
            "identical repeated portraits of the same subject",
            compiled.negative_prompt,
        )
        self.assertIn("audio interaction missing", compiled.negative_prompt)
        self.assertIn("Autonomous composition default", compiled.prompt)
        self.assertIn("Visible-state translation", compiled.prompt)
        self.assertIn(
            "requested emotion or quality not visually readable",
            compiled.negative_prompt,
        )

    def test_autopilot_makes_generic_actions_story_events_without_owner_prompt_craft(self) -> None:
        compiled = compile_visual_prompt(
            request="мальчик бежит за автобусом по мокрой улице",
            kind="image",
        )

        self.assertIn("generic_action", compiled.semantic_flags)
        self.assertIn("another action", compiled.prompt)
        self.assertIn("narrative story-scene", compiled.prompt)
        self.assertIn("Autonomous supporting detail", compiled.prompt)
        self.assertIn("Autonomous lighting default", compiled.prompt)
        self.assertIn("Autonomous detail default", compiled.prompt)
        self.assertIn(
            "static catalog shot when an action was requested",
            compiled.negative_prompt,
        )

    def test_autopilot_does_not_treat_static_russian_nouns_as_actions(self) -> None:
        for request in (
            "работа психолога в светлом современном кабинете",
            "настольная игра на деревянном столе",
            "готовый ужин на красивой тарелке",
            "портрет чиновника в деловом костюме",
        ):
            with self.subTest(request=request):
                compiled = compile_visual_prompt(request=request, kind="image")
                self.assertNotIn("generic_action", compiled.semantic_flags)
                self.assertNotIn(
                    "static catalog shot when an action was requested",
                    compiled.negative_prompt,
                )

    def test_video_transformation_compiles_a_timeline_not_a_static_prompt(self) -> None:
        compiled = compile_visual_prompt(
            request="грязная машина заезжает на мойку и становится блестящей",
            kind="video",
        )

        self.assertIn("transformation", compiled.semantic_flags)
        self.assertIn("8-second", compiled.prompt)
        self.assertIn("opening state", compiled.prompt)
        self.assertIn("interaction or transition", compiled.prompt)
        self.assertIn("final state", compiled.prompt)
        self.assertIn("identity", compiled.prompt)

    def test_plain_static_request_is_not_forced_into_before_after(self) -> None:
        compiled = compile_visual_prompt(
            request="светлая спокойная фотография кабинета психолога без текста",
            kind="image",
        )

        self.assertNotIn("transformation", compiled.semantic_flags)
        self.assertNotIn("listening", compiled.semantic_flags)
        self.assertNotIn("reading", compiled.semantic_flags)
        self.assertNotIn(
            "single-state image with no visible transformation",
            compiled.negative_prompt,
        )
        self.assertIn("scene contract", compiled.prompt)
        self.assertIn("Do not rely on readable text", compiled.prompt)

    def test_explicit_portrait_request_is_not_negated_by_the_compiler(self) -> None:
        compiled = compile_visual_prompt(
            request="деловой портрет психолога в светлом кабинете",
            kind="image",
        )

        self.assertIn("portrait", compiled.semantic_flags)
        self.assertNotIn("generic isolated portrait", compiled.negative_prompt)
        self.assertNotIn(
            "static catalog shot when an action was requested",
            compiled.negative_prompt,
        )

    def test_explicit_text_request_does_not_add_blanket_text_ban(self) -> None:
        compiled = compile_visual_prompt(
            request='афиша с надписью "Открытая встреча"',
            kind="image",
        )

        self.assertIn("explicit_text", compiled.semantic_flags)
        self.assertIn("Readable text is explicitly part", compiled.prompt)
        self.assertNotIn(
            "readable advertising text baked into image",
            compiled.negative_prompt,
        )

    def test_brand_context_stays_contextual_when_other_text_is_requested(self) -> None:
        compiled = compile_visual_prompt(
            request='афиша с надписью "Открытая встреча"',
            kind="image",
            brand_context="Example Wellness: guided resource audio.",
        )

        self.assertIn("explicit_text", compiled.semantic_flags)
        self.assertIn("Readable text is explicitly part", compiled.prompt)
        self.assertIn(
            "Names from business grounding are semantic context only",
            compiled.prompt,
        )
        self.assertIn(
            "unless the owner explicitly requested that exact name as visible text",
            compiled.prompt,
        )

    def test_explicit_transformation_labels_are_not_forbidden(self) -> None:
        compiled = compile_visual_prompt(
            request='коллаж до/после, подпись слева «ДО», справа «ПОСЛЕ»',
            kind="image",
        )

        self.assertIn("transformation", compiled.semantic_flags)
        self.assertIn("explicit_text", compiled.semantic_flags)
        self.assertIn("Readable text is explicitly part", compiled.prompt)
        self.assertIn("unless the owner explicitly requested", compiled.prompt)
        self.assertNotIn(
            "storyboard stage labels, arrows, numbers or captions",
            compiled.negative_prompt,
        )

    def test_resource_audio_transformation_preserves_cause_and_visible_result(self) -> None:
        compiled = compile_visual_prompt(
            request=(
                "ёж, который слушает ресурсные аудио трансы "
                "и становится добрым и пушистым"
            ),
            kind="image",
        )

        self.assertIn("listening", compiled.semantic_flags)
        self.assertIn("transformation", compiled.semantic_flags)
        self.assertIn("visible_state", compiled.semantic_flags)
        self.assertIn("visible audio interaction", compiled.prompt)
        self.assertIn("compact visual storyboard", compiled.prompt)
        self.assertIn("Transformation stage detail", compiled.prompt)
        self.assertIn("paired transformation composition", compiled.prompt)
        self.assertNotIn("Show the subject once", compiled.prompt)
        self.assertNotIn("in-progress softening", compiled.prompt)
        self.assertIn("Never rely on captions", compiled.prompt)
        self.assertIn(
            "storyboard stage labels, arrows, numbers or captions",
            compiled.negative_prompt,
        )
        self.assertIn("audio interaction missing", compiled.negative_prompt)

    def test_owner_can_explicitly_keep_static_transformation_in_one_scene(self) -> None:
        compiled = compile_visual_prompt(
            request=(
                "одна сцена, один кадр: злой кот постепенно становится добрым, "
                "без коллажа и без повторов"
            ),
            kind="image",
        )

        self.assertIn("transformation", compiled.semantic_flags)
        self.assertNotIn("storyboard", compiled.semantic_flags)
        self.assertIn("one coherent scene", compiled.prompt)
        self.assertIn("Show the subject once", compiled.prompt)
        self.assertNotIn("compact visual storyboard", compiled.prompt)


    def test_object_replacement_is_a_constrained_physical_scene(self) -> None:
        compiled = compile_visual_prompt(
            request="Замена раковины",
            kind="image",
        )

        self.assertIn("object_replacement", compiled.semantic_flags)
        self.assertIn("constrained replacement event", compiled.prompt)
        self.assertIn("replace only the requested object", compiled.prompt)
        self.assertIn("installation action", compiled.prompt)
        self.assertIn("before/after", compiled.prompt)
        self.assertIn("do not show only a finished isolated object", compiled.prompt)
        self.assertIn("complete and physically coherent", compiled.prompt)
        self.assertIn("visibly installed and usable", compiled.prompt)
        self.assertIn("essential controls", compiled.prompt)
        self.assertIn(
            "missing essential functional hardware or controls",
            compiled.negative_prompt,
        )
        self.assertIn(
            "unrelated room redesign instead of requested replacement",
            compiled.negative_prompt,
        )

    def test_abstract_style_changes_do_not_enter_physical_replacement_mode(self) -> None:
        for request in (
            "смена настроения на более спокойное",
            "поменять стиль изображения на премиальный",
            "замена цвета фона на синий",
            "replace the visual style with a premium style",
            "swap the background color to blue",
        ):
            with self.subTest(request=request):
                compiled = compile_visual_prompt(request=request, kind="image")
                self.assertNotIn("object_replacement", compiled.semantic_flags)
                self.assertNotIn("constrained replacement event", compiled.prompt)
                self.assertNotIn(
                    "missing essential functional hardware or controls",
                    compiled.negative_prompt,
                )

    def test_physical_replacement_phrasings_still_enter_replacement_mode(self) -> None:
        for request in (
            "поменять раковину на новую",
            "сменить кран в ванной",
            "поменять цветок в вазе",
            "заменить фонарь на новый",
            "заменить трубу и сделать фон светлее",
            "replace the sink with a new one",
            "swap the faucet",
            "replace the car and make the background blue",
        ):
            with self.subTest(request=request):
                compiled = compile_visual_prompt(request=request, kind="image")
                self.assertIn("object_replacement", compiled.semantic_flags)
                self.assertIn("constrained replacement event", compiled.prompt)

    def test_brand_context_is_explicitly_non_renderable_without_text_request(self) -> None:
        compiled = compile_visual_prompt(
            request="уютная сцена прослушивания аудио",
            kind="image",
            brand_context="Brand name: Example Audio Method.",
        )

        self.assertNotIn("explicit_text", compiled.semantic_flags)
        self.assertIn("semantic context only", compiled.prompt)
        self.assertIn("Never render those names", compiled.prompt)
        self.assertIn(
            "readable advertising text baked into image",
            compiled.negative_prompt,
        )

    def test_compiler_is_deterministic_for_retry_and_restart(self) -> None:
        kwargs = {
            "request": "мужчина боится стоматолога, после приёма улыбается",
            "kind": "image",
            "brand_context": "Brand name: Clinic. Tone: calm.",
        }

        first = compile_visual_prompt(**kwargs)
        second = compile_visual_prompt(**kwargs)

        self.assertEqual(first, second)

    def test_compiler_rejects_invalid_kind_and_unbounded_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "image or video"):
            compile_visual_prompt(request="ёж", kind="audio")
        with self.assertRaisesRegex(ValueError, "too long"):
            compile_visual_prompt(request="x" * 1501, kind="image")

    def test_long_transformation_preserves_tail_safety_directives(self) -> None:
        request = (
            "ёж слушает ресурсное аудио и становится добрым и пушистым "
            + ("очень подробно описанная спокойная сцена " * 40)
        )[:1490]
        compiled = compile_visual_prompt(
            request=request,
            kind="image",
            brand_context=(
                "Brand name: Example Wellness. Product: guided audio. "
                + ("Verified business context. " * 30)
            ),
        )

        self.assertIn("compact visual storyboard", compiled.prompt)
        self.assertIn("Do not add fake awards", compiled.prompt)
        self.assertIn("Do not rely on readable text", compiled.prompt)
        self.assertIn("Names from business grounding are semantic context only", compiled.prompt)
        self.assertLessEqual(len(compiled.prompt), 12000)

    def test_semantic_qa_contract_reuses_compiler_flags_for_owner_examples(self) -> None:
        hedgehog = build_visual_semantic_qa_contract(
            request=(
                "ёж, который слушает ресурсные аудио трансы "
                "и становится добрым и пушистым"
            ),
            kind="image",
        )
        self.assertIsNotNone(hedgehog)
        assert hedgehog is not None
        self.assertIn("listening", hedgehog.semantic_flags)
        self.assertIn("transformation", hedgehog.semantic_flags)
        self.assertIn("visible_state", hedgehog.semantic_flags)
        self.assertIn("storyboard", hedgehog.semantic_flags)

        sink = build_visual_semantic_qa_contract(
            request="Замена раковины",
            kind="image",
        )
        self.assertIsNotNone(sink)
        assert sink is not None
        self.assertIn("object_replacement", sink.semantic_flags)
        self.assertNotIn("explicit_text", sink.semantic_flags)
        self.assertIsNone(
            build_visual_semantic_qa_contract(
                request="короткое видео",
                kind="video",
            )
        )

    def test_semantic_qa_contract_mapping_is_strict(self) -> None:
        contract = build_visual_semantic_qa_contract(
            request="Замена раковины",
            kind="image",
        )
        assert contract is not None
        restored = VisualSemanticQAContract.from_mapping(contract.to_mapping())
        self.assertEqual(restored, contract)
        invalid = contract.to_mapping()
        invalid["semantic_flags"] = ["object_replacement", "invented_flag"]
        with self.assertRaisesRegex(ValueError, "semantic QA contract"):
            VisualSemanticQAContract.from_mapping(invalid)

    def test_business_visual_brief_embeds_business_context_into_provider_prompt(
        self,
    ) -> None:
        brief = visual_creatives.build_business_image_brief(
            request=(
                "колючий ёж, который слушает аудиосессию «Тишина» "
                "и превращается в доброго и мягкого"
            ),
            brand_context=(
                "ClientPlatform business brand. "
                "Brand name: Тишина. Tone: calm, supportive."
            ),
            country_code="RU",
        )

        self.assertIn("Brand name: Тишина", brief.prompt)
        self.assertIn("listening unmistakable", brief.prompt)
        self.assertIn("transformation is mandatory visual evidence", brief.prompt)
        self.assertIn("audio interaction missing", brief.negative_prompt)
        self.assertEqual(brief.aspect_ratio, "4:5")

    def test_paid_generation_freezes_the_compiled_prompt_before_consent(self) -> None:
        frozen = visual_creatives.freeze_business_image_payload(
            request=(
                "колючий ёж слушает аудиосессию «Тишина» "
                "и превращается в доброго и мягкого"
            ),
            brand_context="Brand name: Тишина. Tone: calm.",
            country_code="RU",
        )
        payload = json.loads(frozen)
        self.assertEqual(payload["version"], 4)
        self.assertEqual(payload["intent"]["prompt_compiler_version"], 7)
        self.assertEqual(payload["semantic_qa"]["version"], 2)
        self.assertIn("scene_contract", payload["brief"])
        self.assertIsInstance(payload["brief"]["scene_contract"], dict)
        self.assertIsInstance(payload["semantic_qa"]["scene_contract"], dict)
        self.assertEqual(payload["semantic_qa"]["kind"], "image")
        self.assertEqual(payload["semantic_qa"]["country_code"], "RU")
        self.assertEqual(payload["brief"]["country_code"], "RU")
        self.assertIn("listening", payload["semantic_qa"]["semantic_flags"])
        self.assertIn("transformation", payload["semantic_qa"]["semantic_flags"])
        self.assertIn("storyboard", payload["semantic_qa"]["semantic_flags"])
        self.assertIn(
            "same subject identity across stages",
            payload["brief"]["scene_contract"]["required_evidence"],
        )
        provider_prompt = payload["brief"]["prompt"]
        provider_negative = payload["brief"]["negative_prompt"]

        self.assertIn("listening unmistakable", provider_prompt)
        self.assertIn("transformation is mandatory visual evidence", provider_prompt)
        self.assertIn("Brand name: Тишина", provider_prompt)
        self.assertIn("audio interaction missing", provider_negative)

    def test_frozen_payload_uses_gateway_default_country_when_omitted(self) -> None:
        frozen = visual_creatives.freeze_business_image_payload(
            request="Замена раковины",
        )
        payload = json.loads(frozen)

        self.assertEqual(payload["brief"]["country_code"], "RU")
        self.assertEqual(payload["semantic_qa"]["country_code"], "RU")

    def test_legacy_v2_frozen_receipt_never_gains_semantic_qa(self) -> None:
        frozen = visual_creatives.freeze_business_image_payload(
            request="Замена раковины",
            country_code="RU",
        )
        payload = json.loads(frozen)
        payload["version"] = 2
        payload.pop("semantic_qa")
        payload["brief"].pop("scene_contract", None)
        payload["intent"].pop("scene_planner_version", None)
        payload["intent"].pop("scene_planner_source", None)
        payload["intent"].pop("scene_variant", None)
        legacy = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        self.assertEqual(
            visual_creatives.frozen_business_visual_kind(legacy),
            "image",
        )
        self.assertIsNone(
            visual_creatives.frozen_business_visual_semantic_qa(legacy)
        )

    def test_new_video_receipt_explicitly_disables_semantic_qa(self) -> None:
        frozen = visual_creatives.freeze_business_video_payload(
            request="ёж слушает аудио и становится спокойнее",
            country_code="RU",
        )
        payload = json.loads(frozen)

        self.assertEqual(payload["version"], 4)
        self.assertIsNone(payload["semantic_qa"])
        self.assertIsInstance(payload["brief"]["scene_contract"], dict)
        self.assertIsNone(
            visual_creatives.frozen_business_visual_semantic_qa(frozen)
        )


    def test_ad_visual_brief_uses_the_same_semantic_compiler(self) -> None:
        brief = visual_creatives.build_ad_visual_brief(
            title="Тишина",
            body="Человек слушает аудиосессию и становится спокойнее",
            kind="video",
            country_code="RU",
        )

        self.assertIn("vertical advertising video", brief.prompt)
        self.assertIn("Service or offering: Тишина", brief.prompt)
        self.assertIn("listening unmistakable", brief.prompt)
        self.assertIn("unchanged final state", brief.negative_prompt)
        self.assertEqual(brief.aspect_ratio, "9:16")



    def test_presentation_edit_is_not_a_physical_subject_transformation(self) -> None:
        request = (
            "Сделай городскую улицу в стиле акварели, "
            "сохрани людей, здания и композицию"
        )
        compiled = compile_visual_prompt(request=request, kind="image")

        self.assertIn("presentation_change", compiled.semantic_flags)
        self.assertNotIn("transformation", compiled.semantic_flags)
        self.assertIn(
            "The owner is editing visual presentation",
            compiled.prompt,
        )
        self.assertIn(
            "Treat style words as rendering instructions, not as physical properties",
            compiled.prompt,
        )
        self.assertNotIn(
            "The transformation is mandatory visual evidence",
            compiled.prompt,
        )

    def test_presentation_transition_preserves_content_without_subject_mutation(self) -> None:
        request = (
            "Изображение городской улицы постепенно становится акварельным, "
            "архитектура и люди остаются теми же"
        )
        compiled = compile_visual_prompt(request=request, kind="image")

        self.assertIn("presentation_change", compiled.semantic_flags)
        self.assertIn("presentation_transition", compiled.semantic_flags)
        self.assertNotIn("transformation", compiled.semantic_flags)
        self.assertIn(
            "The requested change is a visual-presentation transition",
            compiled.prompt,
        )
        self.assertIn(
            "not a physical mutation of the subject",
            compiled.prompt,
        )
        self.assertNotIn(
            "The transformation is mandatory visual evidence in one coherent scene",
            compiled.prompt,
        )

    def test_static_owner_style_request_does_not_invent_change_event(self) -> None:
        request = "портрет женщины в стиле линогравюры, мягкий боковой свет"
        compiled = compile_visual_prompt(request=request, kind="image")

        self.assertNotIn("presentation_change", compiled.semantic_flags)
        self.assertNotIn("transformation", compiled.semantic_flags)
        self.assertIn(request, compiled.prompt)

    def test_subject_transformation_and_visual_style_can_coexist(self) -> None:
        request = (
            "злой кот постепенно становится добрым, "
            "а стиль изображения меняется на акварельный"
        )
        compiled = compile_visual_prompt(request=request, kind="image")

        self.assertIn("transformation", compiled.semantic_flags)
        self.assertIn("visible_state", compiled.semantic_flags)
        self.assertIn("presentation_change", compiled.semantic_flags)
        self.assertIn(
            "The owner is editing visual presentation",
            compiled.prompt,
        )
        self.assertIn(
            "The transformation is mandatory visual evidence, not optional mood",
            compiled.prompt,
        )


    def test_state_adjective_in_subject_does_not_turn_style_transition_physical(self) -> None:
        request = "The image of a sad dog gradually becomes watercolor"
        compiled = compile_visual_prompt(request=request, kind="image")

        self.assertIn("presentation_change", compiled.semantic_flags)
        self.assertIn("presentation_transition", compiled.semantic_flags)
        self.assertNotIn("transformation", compiled.semantic_flags)
        self.assertNotIn("visible_state", compiled.semantic_flags)
        self.assertIn(
            "The requested change is a visual-presentation transition",
            compiled.prompt,
        )
        self.assertNotIn(
            "The transformation is mandatory visual evidence",
            compiled.prompt,
        )

    def test_subject_transition_does_not_make_static_style_instruction_transition(self) -> None:
        request = (
            "A sad cat gradually becomes happy, "
            "render it in watercolor style"
        )
        compiled = compile_visual_prompt(request=request, kind="image")

        self.assertIn("transformation", compiled.semantic_flags)
        self.assertIn("visible_state", compiled.semantic_flags)
        self.assertIn("presentation_change", compiled.semantic_flags)
        self.assertNotIn("presentation_transition", compiled.semantic_flags)
        self.assertIn(
            "The transformation is mandatory visual evidence, not optional mood",
            compiled.prompt,
        )
        self.assertIn(
            "The owner is editing visual presentation",
            compiled.prompt,
        )
        self.assertNotIn(
            "The requested change is a visual-presentation transition",
            compiled.prompt,
        )

    def test_material_transformation_survives_independent_static_style_clause(self) -> None:
        request = "A stone statue turns into glass, render it in watercolor style"
        compiled = compile_visual_prompt(request=request, kind="image")

        self.assertIn("transformation", compiled.semantic_flags)
        self.assertIn("presentation_change", compiled.semantic_flags)
        self.assertNotIn("presentation_transition", compiled.semantic_flags)

    def test_subject_transition_survives_and_joined_static_style_clause(self) -> None:
        request = (
            "Make a sad cat gradually become happy "
            "and render it in watercolor style"
        )
        compiled = compile_visual_prompt(request=request, kind="image")

        self.assertIn("transformation", compiled.semantic_flags)
        self.assertIn("visible_state", compiled.semantic_flags)
        self.assertIn("presentation_change", compiled.semantic_flags)
        self.assertNotIn("presentation_transition", compiled.semantic_flags)
        self.assertIn(
            "The transformation is mandatory visual evidence in one coherent scene",
            compiled.prompt,
        )

if __name__ == "__main__":
    unittest.main()
