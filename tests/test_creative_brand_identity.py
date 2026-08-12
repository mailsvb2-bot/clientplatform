from __future__ import annotations

from dataclasses import replace

import pytest

from clientplatform.application.creative_studio import (
    build_ad_studio_variants,
    render_idempotency_key,
)
from clientplatform.application.creative_studio_publication import render_format_for_placement
from clientplatform.domain.visual_brand import TenantBrandDNA


def _variant(brand: TenantBrandDNA):
    return build_ad_studio_variants(
        business_id=brand.business_id,
        publication_job_id="brand-sensitive-job",
        title="Консультация",
        body="Спокойно разберём ситуацию и следующий шаг.",
        kind="image",
        brand=brand,
        formats=("square", "feed"),
        country_code="NL",
    )[0]


def test_brand_change_changes_paid_generation_identity():
    first = _variant(TenantBrandDNA(business_id="business-a", tone=("calm",), primary_color="#172033"))
    changed = _variant(TenantBrandDNA(business_id="business-a", tone=("energetic",), primary_color="#172033"))
    assert first.experiment_id != changed.experiment_id
    assert first.variant_id != changed.variant_id


def test_render_idempotency_changes_for_formats_or_composition_only():
    base = _variant(TenantBrandDNA(business_id="business-a"))
    changed_formats = replace(base, formats=("square", "story"))
    changed_composition = replace(base, composition={**base.composition, "layout": "top_card"})
    assert render_idempotency_key(base) != render_idempotency_key(changed_formats)
    assert render_idempotency_key(base) != render_idempotency_key(changed_composition)
    assert render_idempotency_key(base) == render_idempotency_key(base)


def test_current_yandex_placement_selects_canonical_square_and_unknown_fails_closed():
    assert render_format_for_placement("yandex_direct") == "square"
    assert render_format_for_placement(" YANDEX_DIRECT ") == "square"
    with pytest.raises(ValueError, match="unsupported_creative_publication_placement"):
        render_format_for_placement("social_story")
