from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from clientplatform.domain.creative_experiments import stable_experiment_id
from clientplatform.domain.visual_brand import TenantBrandDNA
from services.visual_creative_gateway import (
    VisualCreativeBrief,
    VisualCreativeJob,
    VisualRenderPack,
    poll_visual,
    render_visual_pack,
    submit_visual,
)
from services.visual_gateway_contract import require_render_pack_contract

_ANGLES = (
    ("human_trust", "Human trust: credible real-world professional presence, calm eye-level composition, no staged success symbols."),
    ("clear_process", "Clear process: visually explain a simple next step or service process with calm structure and generous copy space."),
    ("calm_value", "Calm value: show the practical value of the service through an everyday situation, without guarantees or transformation clichés."),
)


def _variant_id(experiment_id: str, angle_id: str) -> str:
    return "cpv_" + hashlib.sha256(f"{experiment_id}|{angle_id}".encode()).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class StudioVariant:
    business_id: str
    experiment_id: str
    variant_id: str
    angle_id: str
    label: str
    kind: str
    prompt: str
    brand_context: str
    formats: tuple[str, ...]
    composition: dict[str, object]
    preflight_score: int
    preflight_issues: tuple[str, ...]
    country_code: str = ""


def build_ad_studio_variants(
    *,
    business_id: str,
    publication_job_id: str,
    title: str,
    body: str,
    kind: str,
    brand: TenantBrandDNA,
    formats: tuple[str, ...] = ("feed", "story", "square"),
    country_code: str = "",
) -> tuple[StudioVariant, ...]:
    visual_kind = str(kind or "image").strip().lower()
    if visual_kind not in {"image", "video"}:
        raise ValueError("kind must be image or video")
    normalized_brand = brand.normalized()
    normalized_brand.assert_business(business_id)
    clean_title = " ".join(str(title or "").split())[:160]
    clean_body = " ".join(str(body or "").split())[:500]
    if not clean_title or not clean_body:
        raise ValueError("creative_title_and_body_required")
    selected_formats = _formats(formats)
    country = str(country_code or "").strip().upper()
    if country and (len(country) != 2 or not country.isalpha()):
        raise ValueError("invalid_creative_country_code")
    creative_fingerprint = hashlib.sha256(
        (f"{clean_title}\n{clean_body}\nbrand:{normalized_brand.fingerprint()}").encode("utf-8")
    ).hexdigest()
    experiment_id = stable_experiment_id(
        business_id=business_id,
        publication_job_id=publication_job_id,
        kind=visual_kind,
        country_code=country,
        creative_fingerprint=creative_fingerprint,
    )
    brand_context = normalized_brand.prompt_context()
    variants: list[StudioVariant] = []
    for index, (angle_id, direction) in enumerate(_ANGLES, start=1):
        variant_id = _variant_id(experiment_id, angle_id)
        prompt = (
            "Create a trustworthy advertising visual for an independent professional or small service business. "
            f"Service: {clean_title}. Context: {clean_body}. Creative direction: {direction} "
            f"{brand_context} "
            "No fake awards, fake reviews, invented statistics, before/after claims, medical guarantees, "
            "money guarantees or manipulative urgency. Do not render readable advertising text in pixels; "
            "the deterministic compositor will apply the real copy."
        )[:12000]
        composition = {
            "headline": clean_title,
            "body": clean_body,
            "cta": "Записаться",
            "layout": "lower_card" if index != 2 else "top_card",
            "brand": normalized_brand.render_brand(),
        }
        score, issues = _preflight(clean_title, clean_body, prompt)
        variants.append(
            StudioVariant(
                business_id=normalized_brand.business_id,
                experiment_id=experiment_id,
                variant_id=variant_id,
                angle_id=angle_id,
                label=f"Вариант {index}",
                kind=visual_kind,
                prompt=prompt,
                brand_context=brand_context,
                formats=selected_formats,
                composition=composition,
                preflight_score=score,
                preflight_issues=issues,
                country_code=country,
            )
        )
    return tuple(variants)


def _formats(values: tuple[str, ...]) -> tuple[str, ...]:
    allowed = {"square", "feed", "story", "landscape"}
    out: list[str] = []
    for item in values:
        token = str(item or "").strip().lower()
        if token not in allowed:
            raise ValueError("invalid_creative_format")
        if token not in out:
            out.append(token)
    if not out or len(out) > 4:
        raise ValueError("creative_formats_required")
    return tuple(out)


def _preflight(title: str, body: str, prompt: str) -> tuple[int, tuple[str, ...]]:
    issues: list[str] = []
    score = 100
    combined = f"{title} {body}".casefold()
    forbidden = (
        "100% гаран", "гарантированно", "гарантия результата", "лучший в мире",
        "до/после", "исцел", "100% guarantee", "guaranteed cure",
        "guaranteed result", "best in the world", "before/after",
    )
    for token in forbidden:
        if token in combined:
            issues.append("risky_claim")
            score -= 35
            break
    if len(title) > 90:
        issues.append("headline_long_for_layout")
        score -= 10
    if len(body) > 300:
        issues.append("body_long_for_layout")
        score -= 10
    if "deterministic compositor" not in prompt:
        issues.append("typography_contract_missing")
        score -= 20
    return max(0, score), tuple(issues)


def render_idempotency_key(variant: StudioVariant) -> str:
    """Bind render idempotency to the exact formats and deterministic composition."""
    payload = json.dumps(
        {"composition": variant.composition, "formats": list(variant.formats)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"clientplatform:{variant.variant_id}:render:{digest}"


def submit_studio_variant(variant: StudioVariant, *, wait_seconds: int = 0) -> tuple[VisualCreativeJob, VisualRenderPack | None]:
    if variant.preflight_score < 70 or "risky_claim" in variant.preflight_issues:
        raise ValueError("unsafe_clientplatform_creative_variant")
    require_render_pack_contract(formats=variant.formats)
    brief = VisualCreativeBrief(
        kind=variant.kind,
        prompt=variant.prompt,
        aspect_ratio="9:16" if variant.kind == "video" else "4:5",
        duration_seconds=8 if variant.kind == "video" else 5,
        brand_context=variant.brand_context,
        country_code=variant.country_code,
    )
    job = submit_visual(
        brief,
        scope_id=variant.business_id,
        idempotency_key=f"clientplatform:{variant.variant_id}:generate",
        wait_seconds=wait_seconds,
    )
    render = _render_when_ready(job, variant)
    return job, render


def poll_studio_variant(job: VisualCreativeJob, variant: StudioVariant) -> tuple[VisualCreativeJob, VisualRenderPack | None]:
    if job.scope_id != variant.business_id:
        raise ValueError("creative_job_business_mismatch")
    current = poll_visual(job.id, scope_id=variant.business_id)
    return current, _render_when_ready(current, variant)


def _render_when_ready(job: VisualCreativeJob, variant: StudioVariant) -> VisualRenderPack | None:
    if job.status != "succeeded" or not job.asset_ready:
        return None
    return render_visual_pack(
        job,
        formats=variant.formats,
        composition=variant.composition,
        idempotency_key=render_idempotency_key(variant),
    )


__all__ = [
    "StudioVariant", "build_ad_studio_variants", "poll_studio_variant",
    "render_idempotency_key", "submit_studio_variant",
]
