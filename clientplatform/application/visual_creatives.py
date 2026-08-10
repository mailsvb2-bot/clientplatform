from __future__ import annotations

from pathlib import Path

from services.visual_creative_gateway import (
    VisualCreativeBrief,
    VisualCreativeGatewayError,
    VisualCreativeJob,
    download_visual,
    poll_visual,
    submit_visual,
)


class VisualCreativeError(RuntimeError):
    """Sanitized failure of the shared visual-creative capability."""


def build_ad_visual_brief(
    *,
    title: str,
    body: str,
    kind: str,
    country_code: str = "",
    preferred_provider: str = "",
) -> VisualCreativeBrief:
    visual_kind = str(kind or "image").strip().lower()
    if visual_kind not in {"image", "video"}:
        raise ValueError("kind must be image or video")
    motion = (
        "Short polished vertical advertising video, natural movement, strong subject "
        "hierarchy and a calm final frame with clean copy space."
        if visual_kind == "video"
        else "Premium advertising key visual, credible real-world lighting, strong "
        "subject hierarchy and generous clean copy space."
    )
    prompt = (
        "Create a trustworthy advertising creative for an independent professional "
        "or small service business. "
        f"Service: {str(title or '').strip()}. Context: {str(body or '').strip()}. "
        f"{motion} "
        "No fake awards, fake reviews, invented statistics, before/after claims, "
        "medical guarantees, money guarantees or manipulative urgency. "
        "Do not bake readable advertising text into the pixels; typography will be "
        "handled separately."
    )
    return VisualCreativeBrief(
        kind=visual_kind,
        prompt=prompt,
        country_code=str(country_code or ""),
        preferred_provider=str(preferred_provider or ""),
        aspect_ratio="4:5" if visual_kind == "image" else "9:16",
        duration_seconds=8,
        brand_context="ClientPlatform: clean, modern, trustworthy, human and useful.",
    )


def create_ad_visual(
    *,
    title: str,
    body: str,
    kind: str,
    scope_id: str,
    idempotency_key: str,
    country_code: str = "",
    preferred_provider: str = "",
    wait_seconds: int = 20,
) -> VisualCreativeJob:
    try:
        return submit_visual(
            build_ad_visual_brief(
                title=title,
                body=body,
                kind=kind,
                country_code=country_code,
                preferred_provider=preferred_provider,
            ),
            scope_id=scope_id,
            idempotency_key=idempotency_key,
            wait_seconds=max(0, min(int(wait_seconds or 0), 60)),
        )
    except VisualCreativeGatewayError as exc:
        raise VisualCreativeError("visual_creative_generation_failed") from exc


def materialize_ad_visual(job: VisualCreativeJob) -> Path:
    try:
        return download_visual(job)
    except (VisualCreativeGatewayError, OSError) as exc:
        raise VisualCreativeError("visual_creative_materialization_failed") from exc


def poll_ad_visual(*, job_id: str, scope_id: str) -> VisualCreativeJob:
    try:
        return poll_visual(job_id, scope_id=scope_id)
    except VisualCreativeGatewayError as exc:
        raise VisualCreativeError("visual_creative_poll_failed") from exc


__all__ = [
    "VisualCreativeError",
    "build_ad_visual_brief",
    "create_ad_visual",
    "materialize_ad_visual",
    "poll_ad_visual",
]
