from __future__ import annotations

import pytest

from clientplatform.application import creative_studio
from services import visual_gateway_contract
from services.visual_creative_gateway import VisualCreativeGatewayError, VisualCreativeJob


def _variant(*, formats: tuple[str, ...] = ("feed", "story", "square")) -> creative_studio.StudioVariant:
    return creative_studio.StudioVariant(
        business_id="business-1",
        experiment_id="experiment-1",
        variant_id="variant-1",
        angle_id="human_trust",
        label="Вариант 1",
        kind="image",
        prompt="Safe deterministic compositor prompt",
        brand_context="",
        formats=formats,
        composition={"headline": "Title", "body": "Body"},
        preflight_score=100,
        preflight_issues=(),
        country_code="RU",
    )


def test_contract_accepts_render_pack_and_normalizes_tokens(monkeypatch) -> None:
    monkeypatch.setattr(
        visual_gateway_contract.visual_gateway,
        "_json",
        lambda *args, **kwargs: {
            "contract_version": "5.1",
            "capabilities": ["generation", "RENDER_PACK", "usage"],
            "render_formats": ["SQUARE", "feed", "story", "landscape"],
        },
    )

    contract = visual_gateway_contract.require_render_pack_contract(
        formats=("feed", "square")
    )

    assert contract.contract_version == "5.1"
    assert "render_pack" in contract.capabilities
    assert {"feed", "square"}.issubset(contract.render_formats)


def test_old_gateway_404_is_normalized_to_render_pack_unavailable(monkeypatch) -> None:
    def old_gateway(*args, **kwargs):
        raise VisualCreativeGatewayError("visual_gateway_http_404")

    monkeypatch.setattr(visual_gateway_contract.visual_gateway, "_json", old_gateway)

    with pytest.raises(
        VisualCreativeGatewayError,
        match="^visual_gateway_render_pack_unavailable$",
    ):
        visual_gateway_contract.require_render_pack_contract(formats=("square",))


def test_contract_rejects_missing_requested_format(monkeypatch) -> None:
    monkeypatch.setattr(
        visual_gateway_contract.visual_gateway,
        "_json",
        lambda *args, **kwargs: {
            "contract_version": "5.1",
            "capabilities": ["generation", "render_pack", "usage"],
            "render_formats": ["square"],
        },
    )

    with pytest.raises(
        VisualCreativeGatewayError,
        match="^visual_gateway_render_format_unavailable$",
    ):
        visual_gateway_contract.require_render_pack_contract(formats=("feed",))


def test_studio_fails_before_paid_submit_when_gateway_is_incompatible(monkeypatch) -> None:
    submit_calls: list[str] = []

    def incompatible(*, formats):
        raise VisualCreativeGatewayError("visual_gateway_render_pack_unavailable")

    monkeypatch.setattr(creative_studio, "require_render_pack_contract", incompatible)
    monkeypatch.setattr(
        creative_studio,
        "submit_visual",
        lambda *args, **kwargs: submit_calls.append("submit") or None,
    )

    with pytest.raises(
        VisualCreativeGatewayError,
        match="^visual_gateway_render_pack_unavailable$",
    ):
        creative_studio.submit_studio_variant(_variant())

    assert submit_calls == []


def test_studio_checks_contract_before_submit(monkeypatch) -> None:
    calls: list[str] = []

    def compatible(*, formats):
        calls.append("preflight")
        return object()

    expected = VisualCreativeJob(
        "job1", "fake", "business-1", "image", "queued"
    )

    def submit(*args, **kwargs):
        calls.append("submit")
        return expected

    monkeypatch.setattr(creative_studio, "require_render_pack_contract", compatible)
    monkeypatch.setattr(creative_studio, "submit_visual", submit)

    job, render = creative_studio.submit_studio_variant(_variant())

    assert calls == ["preflight", "submit"]
    assert job is expected
    assert render is None
