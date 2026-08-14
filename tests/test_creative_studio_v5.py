from __future__ import annotations

import pytest

import clientplatform.application.creative_studio as studio
from clientplatform.application.creative_studio import build_ad_studio_variants, submit_studio_variant
from clientplatform.domain.creative_experiments import VariantPerformance, evaluate_experiment
from clientplatform.domain.visual_brand import TenantBrandDNA


def test_studio_builds_three_stable_tenant_bound_variants():
    brand = TenantBrandDNA(
        business_id="business-a",
        display_name="Практика Анны",
        visual_keywords=("natural light", "calm interior"),
    )
    first = build_ad_studio_variants(
        business_id="business-a",
        publication_job_id="job-1",
        title="Консультация",
        body="Разберём ситуацию и следующий шаг.",
        kind="image",
        brand=brand,
    )
    second = build_ad_studio_variants(
        business_id="business-a",
        publication_job_id="job-1",
        title="Консультация",
        body="Разберём ситуацию и следующий шаг.",
        kind="image",
        brand=brand,
    )
    assert first == second
    assert len(first) == 3
    assert len({item.variant_id for item in first}) == 3
    assert all(item.preflight_score == 100 for item in first)
    assert all("deterministic compositor" in item.prompt for item in first)


def test_studio_identity_changes_when_ad_copy_changes():
    brand = TenantBrandDNA(business_id="business-a")
    first = build_ad_studio_variants(
        business_id="business-a",
        publication_job_id="job-copy",
        title="Консультация",
        body="Первый текст",
        kind="image",
        brand=brand,
    )[0]
    second = build_ad_studio_variants(
        business_id="business-a",
        publication_job_id="job-copy",
        title="Консультация",
        body="Обновлённый текст",
        kind="image",
        brand=brand,
    )[0]
    assert first.experiment_id != second.experiment_id
    assert first.variant_id != second.variant_id


def test_studio_fails_closed_on_cross_tenant_brand():
    try:
        build_ad_studio_variants(
            business_id="business-b",
            publication_job_id="job-1",
            title="Консультация",
            body="Текст",
            kind="image",
            brand=TenantBrandDNA(business_id="business-a"),
        )
    except ValueError as exc:
        assert "business_mismatch" in str(exc)
    else:
        raise AssertionError("cross-tenant brand must fail closed")


def test_experiment_chooses_observed_booking_winner_only_after_sample_floor():
    result = evaluate_experiment(
        (
            VariantPerformance("a", impressions=1000, clicks=70, leads=25, bookings=10, won=4, cost_micros=8_000_000),
            VariantPerformance("b", impressions=1000, clicks=60, leads=30, bookings=18, won=7, cost_micros=10_000_000),
            VariantPerformance("tiny", impressions=30, clicks=20, leads=8, bookings=4, won=1, cost_micros=1_000_000),
        ),
        objective="booking_rate",
        minimum_impressions=100,
    )
    assert result.leader == "b"
    assert result.winner is None
    assert "tiny" not in result.eligible
    assert result.reason == "observed_rate_leader_not_significant"


def test_risky_claim_fails_before_paid_generation(monkeypatch):
    import clientplatform.application.creative_studio as studio
    variant = build_ad_studio_variants(
        business_id="business-a",
        publication_job_id="job-risk",
        title="100% гарантия результата",
        body="Консультация",
        kind="image",
        brand=TenantBrandDNA(business_id="business-a"),
    )[0]
    assert variant.preflight_score < 70
    called = False
    def fake_submit(*args, **kwargs):
        nonlocal called
        called = True
    monkeypatch.setattr(studio, "submit_visual", fake_submit)
    from clientplatform.application.creative_studio import submit_studio_variant
    try:
        submit_studio_variant(variant)
    except ValueError as exc:
        assert "unsafe_clientplatform" in str(exc)
    else:
        raise AssertionError("unsafe creative must fail before paid generation")
    assert called is False


def test_english_guarantee_is_blocked_before_provider(monkeypatch):
    variants = build_ad_studio_variants(
        business_id="business-a",
        publication_job_id="job-a",
        title="Guaranteed cure",
        body="A 100% guarantee of results",
        kind="image",
        brand=TenantBrandDNA(business_id="business-a"),
    )
    called = False

    def forbidden_submit(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(studio, "submit_visual", forbidden_submit)
    with pytest.raises(ValueError, match="unsafe_clientplatform"):
        submit_studio_variant(variants[0])
    assert called is False


def test_country_route_is_bound_into_experiment_and_provider_brief(monkeypatch):
    brand = TenantBrandDNA(business_id="business-a")
    ru = build_ad_studio_variants(
        business_id="business-a", publication_job_id="job-country", title="Консультация", body="Понятный следующий шаг", kind="image", brand=brand, country_code="ru"
    )[0]
    nl = build_ad_studio_variants(
        business_id="business-a", publication_job_id="job-country", title="Консультация", body="Понятный следующий шаг", kind="image", brand=brand, country_code="nl"
    )[0]
    assert ru.experiment_id != nl.experiment_id
    assert ru.variant_id != nl.variant_id
    seen = {}
    def fake_submit(brief, *, scope_id, idempotency_key, wait_seconds=0):
        seen["country"] = brief.country_code
        from services.visual_creative_gateway import VisualCreativeJob
        return VisualCreativeJob("job1", "fake", scope_id, "image", "queued")
    monkeypatch.setattr(studio, "require_render_pack_contract", lambda **kwargs: object())
    monkeypatch.setattr(studio, "submit_visual", fake_submit)
    submit_studio_variant(ru)
    assert seen["country"] == "RU"


def test_experiment_confirms_rate_winner_only_with_95_percent_separation():
    result = evaluate_experiment(
        (
            VariantPerformance("a", impressions=10000, clicks=1600, leads=900, bookings=500, won=220),
            VariantPerformance("b", impressions=10000, clicks=1000, leads=550, bookings=250, won=100),
        ),
        objective="booking_rate",
    )
    assert result.leader == "a"
    assert result.winner == "a"
    assert result.reason == "statistically_supported_observed_rate"


def test_impossible_attribution_funnel_fails_closed():
    with pytest.raises(ValueError, match="funnel_inconsistent"):
        VariantPerformance("bad", impressions=10, clicks=5, leads=2, bookings=3).normalized()
