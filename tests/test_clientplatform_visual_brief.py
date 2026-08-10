from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from clientplatform.application import visual_creatives
from services.visual_creative_gateway import (
    VisualCreativeGatewayError,
    VisualCreativeJob,
)


def test_clientplatform_image_visual_brief_is_presentation_only():
    brief = visual_creatives.build_ad_visual_brief(
        title="Консультация психолога",
        body="Свободное окно во вторник вечером",
        kind="image",
        country_code="RU",
    )
    assert brief.kind == "image"
    assert brief.aspect_ratio == "4:5"
    assert brief.country_code == "RU"
    assert "fake reviews" in brief.prompt
    assert "invented statistics" in brief.prompt
    assert "typography" in brief.prompt


def test_clientplatform_video_visual_brief_preserves_provider_choice():
    brief = visual_creatives.build_ad_visual_brief(
        title="Маркетинговая консультация",
        body="Онлайн встреча",
        kind="video",
        preferred_provider="runway",
    )
    assert brief.kind == "video"
    assert brief.aspect_ratio == "9:16"
    assert brief.duration_seconds == 8
    assert brief.preferred_provider == "runway"
    assert "vertical advertising video" in brief.prompt


def test_invalid_visual_kind_is_rejected():
    with pytest.raises(ValueError, match="image or video"):
        visual_creatives.build_ad_visual_brief(
            title="Service",
            body="Context",
            kind="audio",
        )


def test_create_visual_preserves_scope_and_idempotency():
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
    assert result is expected
    assert submit.call_args.kwargs["scope_id"] == "business-id"
    assert submit.call_args.kwargs["idempotency_key"] == "clientplatform:abcdef12"
    assert submit.call_args.kwargs["wait_seconds"] == 60


def test_gateway_generation_failure_is_normalized():
    with patch.object(
        visual_creatives,
        "submit_visual",
        side_effect=VisualCreativeGatewayError("transport detail"),
    ):
        with pytest.raises(
            visual_creatives.VisualCreativeError,
            match="visual_creative_generation_failed",
        ):
            visual_creatives.create_ad_visual(
                title="Service",
                body="Context",
                kind="image",
                scope_id="business-id",
                idempotency_key="clientplatform:abcdef12",
            )


def test_poll_failure_is_normalized():
    with patch.object(
        visual_creatives,
        "poll_visual",
        side_effect=VisualCreativeGatewayError("transport detail"),
    ):
        with pytest.raises(
            visual_creatives.VisualCreativeError,
            match="visual_creative_poll_failed",
        ):
            visual_creatives.poll_ad_visual(
                job_id="job-1",
                scope_id="business-id",
            )


def test_materialization_failure_is_normalized():
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
        with pytest.raises(
            visual_creatives.VisualCreativeError,
            match="visual_creative_materialization_failed",
        ):
            visual_creatives.materialize_ad_visual(job)


def test_materialization_returns_path():
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
        assert visual_creatives.materialize_ad_visual(job) == expected
