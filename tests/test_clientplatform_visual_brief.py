from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from clientplatform.application import visual_creatives
from services.visual_creative_gateway import (
    VisualCreativeGatewayError,
    VisualCreativeJob,
)


class VisualCreativeApplicationTests(unittest.TestCase):
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
            kind="image",
            status="succeeded",
            asset_ready=True,
        )
        expected = Path("/tmp/creative.jpg")
        with patch.object(visual_creatives, "download_visual", return_value=expected):
            self.assertEqual(visual_creatives.materialize_ad_visual(job), expected)


if __name__ == "__main__":
    unittest.main()
