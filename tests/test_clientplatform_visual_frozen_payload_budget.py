from __future__ import annotations

from clientplatform.application.visual_creatives import freeze_business_image_payload
from clientplatform.application.visual_scene_variants import (
    deterministic_visual_scene_bundle,
    recommended_scene_variant,
)
from clientplatform.domain.visual_prompt_compiler import semantic_flags_for_request
from clientplatform.domain.visual_style_intent import VisualStyleIntent


def _hedgehog_auto_payload(*, brand_context: str) -> str:
    request = (
        "Ёж, который слушает ресурсные аудиотрансы и постепенно "
        "становится добрым и пушистым"
    )
    style = VisualStyleIntent()
    flags = semantic_flags_for_request(request)
    contract, source, variants = deterministic_visual_scene_bundle(
        request=request,
        semantic_flags=flags,
        style_intent=style,
    )
    selected = recommended_scene_variant(variants)
    return freeze_business_image_payload(
        request=request,
        brand_context=brand_context,
        country_code="RU",
        style_intent=style,
        scene_contract=contract,
        scene_planner_source=source,
        scene_variant=selected,
    )


def test_production_hedgehog_auto_payload_fits_durable_receipt_budget() -> None:
    frozen = _hedgehog_auto_payload(brand_context="")
    assert len(frozen) <= 10_000, len(frozen)


def test_supported_brand_context_still_fits_durable_receipt_budget() -> None:
    frozen = _hedgehog_auto_payload(
        brand_context=("Спокойный поддерживающий бренд. " * 40)[:1200],
    )
    assert len(frozen) <= 10_000, len(frozen)
