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
        self.assertEqual(brief.kind, "image")
        self.assertEqual(brief.aspect_ratio, "4:5")
        self.assertEqual(brief.country_code, "RU")
        self.assertIn("Owner request", brief.prompt)
        self.assertIn("кабинета", brief.prompt)
        self.assertEqual(brief.brand_context, "Brand name: Practice. Tone: calm, human.")
        self.assertIn("fake reviews", brief.prompt)

    def test_business_image_request_is_bounded(self) -> None:
        self.assertEqual(
            visual_creatives.normalize_business_image_request("  живая   фотография  "),
            "живая фотография",
        )
        with self.assertRaises(ValueError):
            visual_creatives.normalize_business_image_request(" ")
        with self.assertRaises(ValueError):
            visual_creatives.normalize_business_image_request("x" * 1501)

    def test_create_business_image_reuses_shared_gateway_and_idempotency(self) -> None:
        expected = VisualCreativeJob(
            id="owner-image-1",
            provider="fake",
            scope_id="business-id",
            kind="image",
            status="queued",
        )
        with patch.object(visual_creatives, "submit_visual", return_value=expected) as submit:
            result = visual_creatives.create_business_image(
                request="clean editorial portrait",
                scope_id="business-id",
                idempotency_key="clientplatform:owner-image:abcdef12",
                brand_context="Tone: human.",
                wait_seconds=999,
            )
        self.assertIs(result, expected)
        self.assertEqual(submit.call_args.kwargs["scope_id"], "business-id")
        self.assertEqual(
            submit.call_args.kwargs["idempotency_key"],
            "clientplatform:owner-image:abcdef12",
        )
        self.assertEqual(submit.call_args.kwargs["wait_seconds"], 60)

    def test_invalid_visual_kind_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "image or video"):
            visual_creatives.build_ad_visual_brief(
                title="Service",
                body="Context",
                kind="audio",
            )

    def test_create_visual_preserves_scope_and_idempotency(self) -> None:
        expected = VisualCreativeJob(
            id="job-1",
            provider="fake",
            scope_id="business-id",
            kind="image",
            status="queued",
        )
        with patch.object(
            visual_creatives,
            "submit_visual",
            return_value=expected,
        ) as submit:
            result = visual_creatives.create_ad_visual(
                title="Service",
                body="Context",
                kind="image",
                scope_id="business-id",
                idempotency_key="clientplatform:abcdef12",
                wait_seconds=999,
            )
        self.assertIs(result, expected)
        self.assertEqual(submit.call_args.kwargs["scope_id"], "business-id")
        self.assertEqual(
            submit.call_args.kwargs["idempotency_key"],
            "clientplatform:abcdef12",
        )
        self.assertEqual(submit.call_args.kwargs["wait_seconds"], 60)

    def test_gateway_generation_failure_is_normalized(self) -> None:
        with patch.object(
            visual_creatives,
            "submit_visual",
            side_effect=VisualCreativeGatewayError("transport detail"),
        ):
            with self.assertRaisesRegex(
                visual_creatives.VisualCreativeError,
                "visual_creative_generation_failed",
            ):
                visual_creatives.create_ad_visual(
                    title="Service",
                    body="Context",
                    kind="image",
                    scope_id="business-id",
                    idempotency_key="clientplatform:abcdef12",
                )

    def test_poll_failure_is_normalized(self) -> None:
        with patch.object(
            visual_creatives,
            "poll_visual",
            side_effect=VisualCreativeGatewayError("transport detail"),
        ):
            with self.assertRaisesRegex(
                visual_creatives.VisualCreativeError,
                "visual_creative_poll_failed",
            ):
                visual_creatives.poll_ad_visual(
                    job_id="job-1",
                    scope_id="business-id",
                )

    def test_materialization_failure_is_normalized(self) -> None:
        job = VisualCreativeJob(
            id="job-1",
            provider="fake",
            scope_id="business-id",
            kind="image",
            status="succeeded",
            asset_ready=True,
        )
        with patch.object(
            visual_creatives,
            "download_visual",
            side_effect=OSError("disk detail"),
        ):
            with self.assertRaisesRegex(
                visual_creatives.VisualCreativeError,
                "visual_creative_materialization_failed",
            ):
                visual_creatives.materialize_ad_visual(job)

    def test_materialization_returns_path(self) -> None:
        job = VisualCreativeJob(
            id="job-1",
            provider="fake",
            scope_id="business-id",
            kind="video",
            status="succeeded",
            asset_ready=True,
        )
        expected = Path("/tmp/creative.mp4")
        with patch.object(visual_creatives, "download_visual", return_value=expected):
            self.assertEqual(visual_creatives.materialize_ad_visual(job), expected)

    @unittest.skipUnless(_PIL_AVAILABLE, "Pillow is optional in dependency-light canon")
    def test_materialization_repairs_large_uniform_bottom_band(self) -> None:
        Image = self._pillow_image()
        job = VisualCreativeJob(
            id="job-image",
            provider="yandexart",
            scope_id="business-id",
            kind="image",
            status="succeeded",
            asset_ready=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.png"
            from PIL import Image

            image = Image.new("RGB", (200, 300), (128, 128, 128))
            for x in range(25, 175):
                for y in range(20, 95):
                    image.putpixel((x, y), (20, 25, 30))
            image.save(path)
            with patch.object(visual_creatives, "download_visual", return_value=path):
                result = visual_creatives.materialize_ad_visual(job)
            with Image.open(result) as repaired:
                self.assertLess(repaired.height, 180)
                self.assertLess(repaired.width, 200)

    @unittest.skipUnless(_PIL_AVAILABLE, "Pillow is optional in dependency-light canon")
    def test_materialization_keeps_moderate_intentional_copy_space(self) -> None:
        Image = self._pillow_image()
        job = VisualCreativeJob(
            id="job-image",
            provider="fake",
            scope_id="business-id",
            kind="image",
            status="succeeded",
            asset_ready=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.png"
            from PIL import Image

            image = Image.new("RGB", (200, 300), (128, 128, 128))
            for x in range(20, 180):
                for y in range(20, 195):
                    image.putpixel((x, y), (20, 25, 30))
            image.save(path)
            with patch.object(visual_creatives, "download_visual", return_value=path):
                result = visual_creatives.materialize_ad_visual(job)
            with Image.open(result) as repaired:
                self.assertEqual(repaired.size, (200, 300))

    @unittest.skipUnless(_PIL_AVAILABLE, "Pillow is optional in dependency-light canon")
    def test_materialization_can_preserve_large_intentional_copy_space(self) -> None:
        Image = self._pillow_image()
        job = VisualCreativeJob(
            id="job-image",
            provider="fake",
            scope_id="business-id",
            kind="image",
            status="succeeded",
            asset_ready=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.png"
            from PIL import Image

            image = Image.new("RGB", (200, 300), (128, 128, 128))
            for x in range(25, 175):
                for y in range(20, 95):
                    image.putpixel((x, y), (20, 25, 30))
            image.save(path)
            with patch.object(visual_creatives, "download_visual", return_value=path):
                result = visual_creatives.materialize_ad_visual(
                    job,
                    repair_blank_bands=False,
                )
            with Image.open(result) as repaired:
                self.assertEqual(repaired.size, (200, 300))


    @unittest.skipUnless(_PIL_AVAILABLE, "Pillow is optional in dependency-light canon")
    def test_materialization_crops_large_transparent_padding(self) -> None:
        Image = self._pillow_image()
        job = VisualCreativeJob(
            id="job-image",
            provider="yandexart",
            scope_id="business-id",
            kind="image",
            status="succeeded",
            asset_ready=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.png"
            from PIL import Image

            image = Image.new("RGBA", (200, 300), (0, 0, 0, 0))
            for x in range(20, 180):
                for y in range(20, 100):
                    image.putpixel((x, y), (40, 50, 60, 255))
            image.save(path)
            with patch.object(visual_creatives, "download_visual", return_value=path):
                result = visual_creatives.materialize_ad_visual(job)
            with Image.open(result) as repaired:
                self.assertEqual(repaired.mode, "RGB")
                self.assertEqual(repaired.size, (160, 80))

    def test_materialization_rejects_excessive_decoded_dimensions(self) -> None:
        self._pillow_image()
        fake = type("FakeImage", (), {"size": (8193, 64)})()
        with self.assertRaisesRegex(
            visual_creatives.VisualCreativeError,
            "visual_creative_invalid_image_dimensions",
        ):
            visual_creatives._validate_materialized_image_dimensions(fake)

    def test_materialization_normalizes_decompression_bomb_error(self) -> None:
        Image = self._pillow_image()
        job = VisualCreativeJob(
            id="job-image",
            provider="fake",
            scope_id="business-id",
            kind="image",
            status="succeeded",
            asset_ready=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.png"
            Image.new("RGB", (100, 100), (30, 40, 50)).save(path)
            original_limit = Image.MAX_IMAGE_PIXELS
            try:
                Image.MAX_IMAGE_PIXELS = 1000
                with patch.object(visual_creatives, "download_visual", return_value=path):
                    with self.assertRaisesRegex(
                        visual_creatives.VisualCreativeError,
                        "visual_creative_invalid_image_asset",
                    ):
                        visual_creatives.materialize_ad_visual(job)
            finally:
                Image.MAX_IMAGE_PIXELS = original_limit

    def test_materialization_preserves_webp_bytes_for_webp_path(self) -> None:
        Image = self._pillow_image()
        if "WEBP" not in Image.registered_extensions().values():
            self.skipTest("Pillow WebP codec is unavailable")
        job = VisualCreativeJob(
            id="job-image",
            provider="fake",
            scope_id="business-id",
            kind="image",
            status="succeeded",
            asset_ready=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.webp"
            Image.new("RGB", (128, 128), (30, 40, 50)).save(path, format="WEBP")
            with patch.object(visual_creatives, "download_visual", return_value=path):
                result = visual_creatives.materialize_ad_visual(job)
            with Image.open(result) as repaired:
                self.assertEqual(repaired.format, "WEBP")
                self.assertEqual(repaired.size, (128, 128))

    @unittest.skipUnless(_PIL_AVAILABLE, "Pillow is optional in dependency-light canon")
    def test_materialization_rejects_non_image_payload(self) -> None:
        self._pillow_image()
        job = VisualCreativeJob(
            id="job-image",
            provider="fake",
            scope_id="business-id",
            kind="image",
            status="succeeded",
            asset_ready=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.png"
            path.write_bytes(b"not-an-image")
            with patch.object(visual_creatives, "download_visual", return_value=path):
                with self.assertRaisesRegex(
                    visual_creatives.VisualCreativeError,
                    "visual_creative_invalid_image_asset",
                ):
                    visual_creatives.materialize_ad_visual(job)


if __name__ == "__main__":
    unittest.main()