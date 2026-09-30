from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from clientplatform.application import visual_creatives
from clientplatform.domain.visual_prompt_compiler import compile_visual_prompt
from clientplatform.domain.visual_style_intent import (
    VisualStyleIntent,
    infer_visual_style_intent,
    resolve_visual_style_intent,
    visual_style_preset,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.infrastructure.visual_style_preference_repository import (
    VisualStylePreferenceRepository,
)
from services.db.schema import clientplatform_creative_experiments, clientplatform_tenancy


class VisualStyleIntentTests(unittest.TestCase):
    def test_inference_reads_explicit_human_style_words(self) -> None:
        result = infer_visual_style_intent(
            "тёплая мягкая реалистичная спокойная сцена, крупный план"
        )

        self.assertIn("color_temperature", result.explicit_fields)
        self.assertIn("emotional_tone", result.explicit_fields)
        self.assertIn("realism", result.explicit_fields)
        self.assertIn("contrast", result.explicit_fields)
        self.assertIn("composition", result.explicit_fields)
        self.assertEqual(result.intent.color_temperature, "warm")
        self.assertEqual(result.intent.emotional_tone, "calm")
        self.assertEqual(result.intent.realism, "realistic")
        self.assertEqual(result.intent.contrast, "soft")
        self.assertEqual(result.intent.composition, "close_up")

    def test_priority_is_saved_then_request_then_manual_selection(self) -> None:
        saved = visual_style_preset("cinematic")
        selected = VisualStyleIntent(
            color_temperature="warm",
            emotional_tone="friendly",
        )

        result = resolve_visual_style_intent(
            request="холодная драматичная сцена",
            saved=saved,
            selected=selected,
        )

        self.assertEqual(result.color_temperature, "warm")
        self.assertEqual(result.emotional_tone, "friendly")
        self.assertEqual(result.lighting, "dark")
        self.assertEqual(result.contrast, "strong")

    def test_style_json_is_versioned_and_rejects_unknown_fields(self) -> None:
        style = visual_style_preset("soft_calm")
        self.assertEqual(VisualStyleIntent.from_json(style.to_json()), style)

        payload = json.loads(style.to_json())
        payload["style"]["unknown"] = "value"
        with self.assertRaisesRegex(ValueError, "unknown"):
            VisualStyleIntent.from_json(json.dumps(payload))

    def test_compiler_keeps_semantics_above_style(self) -> None:
        compiled = compile_visual_prompt(
            request=(
                "колючий ёж слушает аудиосессию и превращается "
                "в доброго и мягкого"
            ),
            kind="image",
            style_intent=VisualStyleIntent(
                color_temperature="warm",
                emotional_tone="friendly",
                composition="close_up",
            ),
        )

        self.assertIn("transformation is mandatory visual evidence", compiled.prompt)
        self.assertIn("listening unmistakable", compiled.prompt)
        self.assertIn("Use a warm color temperature", compiled.prompt)
        self.assertIn("friendly and approachable", compiled.prompt)
        self.assertIn("Use a close-up composition", compiled.prompt)
        self.assertIn(
            "Style choices may shape presentation but must never remove",
            compiled.prompt,
        )

    def test_frozen_paid_payload_contains_style_snapshot_and_is_deterministic(self) -> None:
        style = visual_style_preset("warm_friendly")
        first = visual_creatives.freeze_business_image_payload(
            request="ёж слушает аудиосессию",
            brand_context="Brand name: Audio practice.",
            style_intent=style,
        )
        second = visual_creatives.freeze_business_image_payload(
            request="ёж слушает аудиосессию",
            brand_context="Brand name: Audio practice.",
            style_intent=style,
        )

        self.assertEqual(first, second)
        payload = json.loads(first)
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["intent"]["style_schema_version"], 1)
        self.assertEqual(payload["intent"]["style"]["color_temperature"], "warm")
        self.assertIn("Use a warm color temperature", payload["brief"]["prompt"])

    def test_worst_case_frozen_payload_stays_within_receipt_contract(self) -> None:
        payload = visual_creatives.freeze_business_image_payload(
            request="сцена " + ("деталь " * 180),
            brand_context="Brand context " + ("visual " * 220),
            style_intent=VisualStyleIntent(
                color_temperature="warm",
                emotional_tone="dramatic",
                energy="high",
                realism="photorealistic",
                lighting="dark",
                contrast="strong",
                detail="detailed",
                composition="story_scene",
                motion="dynamic",
                commercial_tone="premium",
                copy_space="large",
            ),
        )

        self.assertLessEqual(len(payload), 10000)

    def test_legacy_version_one_frozen_payload_stays_loadable(self) -> None:
        current = json.loads(
            visual_creatives.freeze_business_image_payload(request="calm office")
        )
        current["version"] = 1
        current.pop("intent")
        legacy = json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected = type("Job", (), {"id": "job-1"})()

        with patch.object(visual_creatives, "submit_visual", return_value=expected) as submit:
            result = visual_creatives.create_business_image_from_frozen_payload(
                provider_payload_json=legacy,
                scope_id="scope-1",
                idempotency_key="legacy-stable-key",
            )

        self.assertIs(result, expected)
        self.assertIn("calm office", submit.call_args.args[0].prompt)


class VisualStylePreferenceRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_creative_experiments.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Style business")
        self.actor = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.repo = VisualStylePreferenceRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_missing_preference_is_auto_and_saved_preference_roundtrips(self) -> None:
        self.assertEqual(self.repo.get(actor=self.actor), VisualStyleIntent())

        style = visual_style_preset("premium")
        saved = self.repo.save(
            actor=self.actor,
            style=style,
            now="2026-09-30T10:00:00+00:00",
        )

        self.assertEqual(saved, style)
        self.assertEqual(self.repo.get(actor=self.actor), style)
        self.assertTrue(self.repo.clear(actor=self.actor))
        self.assertEqual(self.repo.get(actor=self.actor), VisualStyleIntent())

    def test_schema_contains_no_media_bytes_or_prompt_history(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(visual_style_preferences)"
            ).fetchall()
        }
        self.assertEqual(
            columns,
            {
                "business_id",
                "created_by_member_id",
                "style_json",
                "created_at",
                "updated_at",
            },
        )


if __name__ == "__main__":
    unittest.main()
