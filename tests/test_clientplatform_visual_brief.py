from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from clientplatform.application import visual_creatives
_PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None

from services.visual_creative_gateway import (
    VisualCreativeGatewayError,
    VisualCreativeJob,
)


class VisualCreativeApplicationTests(unittest.TestCase):
    def _pillow_image(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed in dependency-light Canon")
        return Image

    def test_clientplatform_image_visual_brief_is_presentation_only(self) -> None:
        brief = visual_creatives.build_ad_visual_brief(
            title="Консультация психолога",
            body="Свободное окно во вторник вечером",
            kind="image",
            country_code="RU",
        )
        self.assertEqual(brief.kind, "image")
        self.assertEqual(brief.aspect_ratio, "4:5")
        self.assertEqual(brief.country_code, "RU")
        self.assertIn("fake reviews", brief.prompt)
        self.assertIn("fully inside the frame", brief.prompt)
        self.assertIn("invented statistics", brief.prompt)
        self.assertIn("typography", brief.prompt)

    def test_clientplatform_video_visual_brief_preserves_provider_choice(self) -> None:
        brief = visual_creatives.build_ad_visual_brief(
            title="Маркетинговая консультация",
            body="Онлайн встреча",
            kind="video",
            preferred_provider="runway",
        )
        self.assertEqual(brief.kind, "video")
        self.assertEqual(brief.aspect_ratio, "9:16")
        self.assertEqual(brief.duration_seconds, 8)
        self.assertEqual(brief.preferred_provider, "runway")
        self.assertIn("vertical advertising video", brief.prompt)

    def test_video_generation_mode_is_exposed_without_provider_details(self) -> None:
        with patch.object(
            visual_creatives,
            "configured_visual_video_mode",
            return_value="motion",
        ):
            self.assertEqual(
                visual_creatives.visual_video_generation_mode(country_code="RU"),
                "motion",
            )

    def test_video_generation_mode_normalizes_gateway_failure(self) -> None:
        with patch.object(
            visual_creatives,
            "configured_visual_video_mode",
            side_effect=VisualCreativeGatewayError("secret transport detail"),
        ):
            with self.assertRaisesRegex(
                visual_creatives.VisualCreativeError,
                "visual_creative_provider_preflight_failed",
            ):
                visual_creatives.visual_video_generation_mode(country_code="RU")

    def test_business_image_brief_accepts_plain_owner_language_and_brand(self) -> None:
        brief = visual_creatives.build_business_image_brief(
            request="спокойная реалистичная фотография кабинета без текста",
            brand_context="Brand name: Practice. Tone: calm, human.",
            country_code="RU",
        )