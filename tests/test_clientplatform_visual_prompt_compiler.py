from __future__ import annotations

import json
import unittest

from clientplatform.application import visual_creatives
from clientplatform.domain.visual_prompt_compiler import compile_visual_prompt


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
            ("transformation", "listening"),
        )
        self.assertIn(request, compiled.prompt)
        self.assertIn("listening unmistakable", compiled.prompt)
        self.assertIn("headphones", compiled.prompt)
        self.assertIn("transformation is mandatory visual evidence", compiled.prompt)
        self.assertIn("both the initial and final states", compiled.prompt)
        self.assertIn("same subject", compiled.prompt)
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
        self.assertIn("audio interaction missing", compiled.negative_prompt)

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
        provider_prompt = payload["brief"]["prompt"]
        provider_negative = payload["brief"]["negative_prompt"]

        self.assertIn("listening unmistakable", provider_prompt)
        self.assertIn("transformation is mandatory visual evidence", provider_prompt)
        self.assertIn("Brand name: Тишина", provider_prompt)
        self.assertIn("audio interaction missing", provider_negative)

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
        self.assertIn("transformation", brief.negative_prompt)
        self.assertEqual(brief.aspect_ratio, "9:16")


if __name__ == "__main__":
    unittest.main()
