from __future__ import annotations

import json
from pathlib import Path

from services.visual_creative_gateway import (
    VisualCreativeBrief,
    VisualCreativeGatewayError,
    VisualCreativeJob,
    configured_visual_providers,
    download_visual,
    poll_visual,
    submit_visual,
)


class VisualCreativeError(RuntimeError):
    """Sanitized failure of the shared visual-creative capability."""


_BUSINESS_IMAGE_BRIEF_VERSION = 1
_BUSINESS_IMAGE_WAIT_SECONDS = 20
_FROZEN_BRIEF_KEYS = frozenset(
    {
        "kind",
        "prompt",
        "country_code",
        "preferred_provider",
        "aspect_ratio",
        "duration_seconds",
        "negative_prompt",
        "reference_url",
        "brand_context",
        "seed",
    }
)


def _brief_dict(brief: VisualCreativeBrief) -> dict[str, object]:
    return {
        "kind": brief.kind,
        "prompt": brief.prompt,
        "country_code": brief.country_code,
        "preferred_provider": brief.preferred_provider,
        "aspect_ratio": brief.aspect_ratio,
        "duration_seconds": brief.duration_seconds,
        "negative_prompt": brief.negative_prompt,
        "reference_url": brief.reference_url,
        "brand_context": brief.brand_context,
        "seed": brief.seed,
    }


def visual_generation_ready(
    *,
    kind: str,
    country_code: str = "",
) -> bool:
    """Return deployment readiness without exposing provider credentials."""

    try:
        return bool(
            configured_visual_providers(
                kind,
                country_code=country_code,
            )
        )
    except VisualCreativeGatewayError as exc:
        raise VisualCreativeError("visual_creative_provider_preflight_failed") from exc


def freeze_business_visual_payload(
    *,
    request: str,
    kind: str,
    brand_context: str = "",
    country_code: str = "",
    preferred_provider: str = "",
    binding: dict[str, str] | None = None,
) -> str:
    """Freeze the exact versioned image/video brief before owner paid consent."""

    brief = build_business_visual_brief(
        request=request,
        kind=kind,
        brand_context=brand_context,
        country_code=country_code,
        preferred_provider=preferred_provider,
    )
    value: dict[str, object] = {
        "version": _BUSINESS_IMAGE_BRIEF_VERSION,
        "brief": _brief_dict(brief),
        "wait_seconds": _BUSINESS_IMAGE_WAIT_SECONDS,
    }
    if binding is not None:
        normalized_binding = {str(key): str(item) for key, item in binding.items()}
        required = {"type", "event_id", "stage", "slot_key", "kind"}
        if set(normalized_binding) != required:
            raise ValueError("frozen business visual binding is invalid")
        if normalized_binding["type"] != "event_content":
            raise ValueError("frozen business visual binding is invalid")
        if normalized_binding["kind"] != brief.kind:
            raise ValueError("frozen business visual binding kind mismatch")
        if any(
            not value or len(value) > 200 or any(ord(char) < 32 for char in value)
            for value in normalized_binding.values()
        ):
            raise ValueError("frozen business visual binding is invalid")
        value["binding"] = normalized_binding
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def freeze_business_image_payload(
    *,
    request: str,
    brand_context: str = "",
    country_code: str = "",
    preferred_provider: str = "",
) -> str:
    return freeze_business_visual_payload(
        request=request,
        kind="image",
        brand_context=brand_context,
        country_code=country_code,
        preferred_provider=preferred_provider,
    )


def freeze_business_video_payload(
    *,
    request: str,
    brand_context: str = "",
    country_code: str = "",
    preferred_provider: str = "",
) -> str:
    return freeze_business_visual_payload(
        request=request,
        kind="video",
        brand_context=brand_context,
        country_code=country_code,
        preferred_provider=preferred_provider,
    )


def _load_frozen_business_visual_payload(value: str) -> tuple[VisualCreativeBrief, int]:
    try:
        raw = json.loads(str(value or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("frozen business image payload is invalid") from exc
    if not isinstance(raw, dict) or set(raw) not in (
        {"version", "brief", "wait_seconds"},
        {"version", "brief", "wait_seconds", "binding"},
    ):
        raise ValueError("frozen business image payload is invalid")
    if raw.get("version") != _BUSINESS_IMAGE_BRIEF_VERSION:
        raise ValueError("unsupported frozen business image payload version")
    raw_brief = raw.get("brief")
    if not isinstance(raw_brief, dict) or set(raw_brief) != _FROZEN_BRIEF_KEYS:
        raise ValueError("frozen business image brief is invalid")
    wait_seconds = raw.get("wait_seconds")
    if not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 60:
        raise ValueError("frozen business image wait is invalid")
    seed = raw_brief.get("seed")
    if seed is not None and not isinstance(seed, int):
        raise ValueError("frozen business image seed is invalid")
    duration = raw_brief.get("duration_seconds")
    if not isinstance(duration, int):
        raise ValueError("frozen business image duration is invalid")
    brief = VisualCreativeBrief(
        kind=str(raw_brief.get("kind") or ""),
        prompt=str(raw_brief.get("prompt") or ""),
        country_code=str(raw_brief.get("country_code") or ""),
        preferred_provider=str(raw_brief.get("preferred_provider") or ""),
        aspect_ratio=str(raw_brief.get("aspect_ratio") or ""),
        duration_seconds=duration,
        negative_prompt=str(raw_brief.get("negative_prompt") or ""),
        reference_url=str(raw_brief.get("reference_url") or ""),
        brand_context=str(raw_brief.get("brand_context") or ""),
        seed=seed,
    )
    if brief.kind not in {"image", "video"} or not brief.prompt.strip():
        raise ValueError("frozen business visual brief is invalid")
    return brief, wait_seconds


def _load_frozen_business_image_payload(value: str) -> tuple[VisualCreativeBrief, int]:
    brief, wait_seconds = _load_frozen_business_visual_payload(value)
    if brief.kind != "image":
        raise ValueError("frozen business image brief is invalid")
    return brief, wait_seconds


def frozen_business_visual_kind(value: str) -> str:
    brief, _ = _load_frozen_business_visual_payload(value)
    return brief.kind


def frozen_business_visual_binding(value: str) -> dict[str, str] | None:
    raw = json.loads(str(value or ""))
    if not isinstance(raw, dict):
        raise ValueError("frozen business visual payload is invalid")
    binding = raw.get("binding")
    if binding is None:
        # Legacy image receipts predate event-content bindings and may carry an
        # older frozen payload shape. Delivery/recovery of those receipts must
        # not be blocked merely because there is no event binding to persist.
        return None
    _load_frozen_business_visual_payload(value)
    if not isinstance(binding, dict):
        raise ValueError("frozen business visual binding is invalid")
    normalized = {str(key): str(item) for key, item in binding.items()}
    required = {"type", "event_id", "stage", "slot_key", "kind"}
    if set(normalized) != required or normalized["type"] != "event_content":
        raise ValueError("frozen business visual binding is invalid")
    if normalized["kind"] != frozen_business_visual_kind(value):
        raise ValueError("frozen business visual binding kind mismatch")
    return normalized


def normalize_business_image_request(value: str) -> str:
    request = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if not request:
        raise ValueError("business image request must not be empty")
    if len(request) > 1500:
        raise ValueError("business image request is too long")
    if any(ord(char) < 32 for char in request):
        raise ValueError("business image request contains control characters")
    return request


def build_business_visual_brief(
    *,
    request: str,
    kind: str,
    brand_context: str = "",
    country_code: str = "",
    preferred_provider: str = "",
) -> VisualCreativeBrief:
    owner_request = normalize_business_image_request(request)
    visual_kind = str(kind or "").strip().lower()
    if visual_kind not in {"image", "video"}:
        raise ValueError("business visual kind must be image or video")
    medium = (
        "Create one polished short vertical video for an independent professional or small business. "
        "Use natural motion, a clear subject and a calm final frame. "
        if visual_kind == "video"
        else "Create one polished visual for an independent professional or small business. "
    )
    prompt = (
        medium
        + f"Owner request: {owner_request}. "
        "Use credible natural details, human proportions and realistic lighting. "
        "No fake awards, fake reviews, invented statistics, before/after claims, "
        "medical guarantees, money guarantees or manipulative urgency. "
        "Do not bake readable advertising text into the pixels unless the owner "
        "explicitly asked for text as part of the visual concept."
    )
    return VisualCreativeBrief(
        kind=visual_kind,
        prompt=prompt,
        country_code=str(country_code or ""),
        preferred_provider=str(preferred_provider or ""),
        aspect_ratio="9:16" if visual_kind == "video" else "4:5",
        duration_seconds=8,
        brand_context=str(brand_context or "").strip()[:2500],
    )


def build_business_image_brief(
    *,
    request: str,
    brand_context: str = "",
    country_code: str = "",
    preferred_provider: str = "",
) -> VisualCreativeBrief:
    return build_business_visual_brief(
        request=request,
        kind="image",
        brand_context=brand_context,
        country_code=country_code,
        preferred_provider=preferred_provider,
    )


def create_business_visual_from_frozen_payload(
    *,
    provider_payload_json: str,
    scope_id: str,
    idempotency_key: str,
) -> VisualCreativeJob:
    brief, wait_seconds = _load_frozen_business_visual_payload(provider_payload_json)
    try:
        return submit_visual(
            brief,
            scope_id=scope_id,
            idempotency_key=idempotency_key,
            wait_seconds=wait_seconds,
        )
    except VisualCreativeGatewayError as exc:
        raise VisualCreativeError("visual_creative_generation_failed") from exc


def create_business_image_from_frozen_payload(
    *,
    provider_payload_json: str,
    scope_id: str,
    idempotency_key: str,
) -> VisualCreativeJob:
    brief, _ = _load_frozen_business_image_payload(provider_payload_json)
    del brief
    return create_business_visual_from_frozen_payload(
        provider_payload_json=provider_payload_json,
        scope_id=scope_id,
        idempotency_key=idempotency_key,
    )


def create_business_video_from_frozen_payload(
    *,
    provider_payload_json: str,
    scope_id: str,
    idempotency_key: str,
) -> VisualCreativeJob:
    brief, _ = _load_frozen_business_visual_payload(provider_payload_json)
    if brief.kind != "video":
        raise ValueError("frozen business video brief is invalid")
    return create_business_visual_from_frozen_payload(
        provider_payload_json=provider_payload_json,
        scope_id=scope_id,
        idempotency_key=idempotency_key,
    )


def create_business_image(
    *,
    request: str,
    scope_id: str,
    idempotency_key: str,
    brand_context: str = "",
    country_code: str = "",
    preferred_provider: str = "",
    wait_seconds: int = 20,
) -> VisualCreativeJob:
    # Compatibility path for existing callers. Restart-safe owner generation freezes
    # the provider brief first and calls create_business_image_from_frozen_payload.
    payload = freeze_business_image_payload(
        request=request,
        brand_context=brand_context,
        country_code=country_code,
        preferred_provider=preferred_provider,
    )
    if int(wait_seconds or 0) != _BUSINESS_IMAGE_WAIT_SECONDS:
        brief, _ = _load_frozen_business_image_payload(payload)
        try:
            return submit_visual(
                brief,
                scope_id=scope_id,
                idempotency_key=idempotency_key,
                wait_seconds=max(0, min(int(wait_seconds or 0), 60)),
            )
        except VisualCreativeGatewayError as exc:
            raise VisualCreativeError("visual_creative_generation_failed") from exc
    return create_business_image_from_frozen_payload(
        provider_payload_json=payload,
        scope_id=scope_id,
        idempotency_key=idempotency_key,
    )


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


def materialize_ad_visual(
    job: VisualCreativeJob, *, output_dir: str | None = None
) -> Path:
    try:
        return download_visual(job, output_dir=output_dir)
    except (VisualCreativeGatewayError, OSError) as exc:
        raise VisualCreativeError("visual_creative_materialization_failed") from exc


def poll_ad_visual(*, job_id: str, scope_id: str) -> VisualCreativeJob:
    try:
        return poll_visual(job_id, scope_id=scope_id)
    except VisualCreativeGatewayError as exc:
        raise VisualCreativeError("visual_creative_poll_failed") from exc


__all__ = [
    "VisualCreativeError",
    "visual_generation_ready",
    "build_business_visual_brief",
    "build_business_image_brief",
    "create_business_visual_from_frozen_payload",
    "create_business_image",
    "create_business_image_from_frozen_payload",
    "create_business_video_from_frozen_payload",
    "freeze_business_visual_payload",
    "freeze_business_image_payload",
    "freeze_business_video_payload",
    "frozen_business_visual_kind",
    "frozen_business_visual_binding",
    "normalize_business_image_request",
    "build_ad_visual_brief",
    "create_ad_visual",
    "materialize_ad_visual",
    "poll_ad_visual",
]
