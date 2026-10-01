from __future__ import annotations

import json
from pathlib import Path
import os

from clientplatform.domain.visual_prompt_compiler import compile_visual_prompt
from clientplatform.domain.visual_style_intent import (
    STYLE_SCHEMA_VERSION,
    SUPPORTED_STYLE_SCHEMA_VERSIONS,
    VisualStyleIntent,
    resolve_visual_style_intent,
)

from services.visual_creative_gateway import (
    VisualCreativeBrief,
    VisualCreativeGatewayError,
    VisualCreativeJob,
    configured_visual_providers,
    configured_visual_video_mode,
    download_visual,
    poll_visual,
    submit_visual,
    wait_visual,
)


class VisualCreativeError(RuntimeError):
    """Sanitized failure of the shared visual-creative capability."""


_BUSINESS_IMAGE_BRIEF_VERSION = 2
_SUPPORTED_BUSINESS_IMAGE_BRIEF_VERSIONS = frozenset({1, 2})
_PROMPT_COMPILER_VERSION = 2
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


def visual_video_generation_mode(*, country_code: str = "") -> str:
    """Return native/motion/unavailable for owner-facing video UX."""

    try:
        return configured_visual_video_mode(country_code=country_code)
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
    style_intent: VisualStyleIntent | None = None,
) -> str:
    """Freeze the exact versioned image/video brief before owner paid consent."""

    resolved_style = resolve_visual_style_intent(
        request=request,
        selected=style_intent,
    )
    brief = build_business_visual_brief(
        request=request,
        kind=kind,
        brand_context=brand_context,
        country_code=country_code,
        preferred_provider=preferred_provider,
        style_intent=resolved_style,
    )
    value: dict[str, object] = {
        "version": _BUSINESS_IMAGE_BRIEF_VERSION,
        "brief": _brief_dict(brief),
        "wait_seconds": _BUSINESS_IMAGE_WAIT_SECONDS,
        "intent": {
            "prompt_compiler_version": _PROMPT_COMPILER_VERSION,
            "style_schema_version": STYLE_SCHEMA_VERSION,
            "style": resolved_style.to_mapping(),
        },
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
    style_intent: VisualStyleIntent | None = None,
) -> str:
    return freeze_business_visual_payload(
        request=request,
        kind="image",
        brand_context=brand_context,
        country_code=country_code,
        preferred_provider=preferred_provider,
        style_intent=style_intent,
    )


def freeze_business_video_payload(
    *,
    request: str,
    brand_context: str = "",
    country_code: str = "",
    preferred_provider: str = "",
    style_intent: VisualStyleIntent | None = None,
) -> str:
    return freeze_business_visual_payload(
        request=request,
        kind="video",
        brand_context=brand_context,
        country_code=country_code,
        preferred_provider=preferred_provider,
        style_intent=style_intent,
    )


def _load_frozen_business_visual_payload(value: str) -> tuple[VisualCreativeBrief, int]:
    try:
        raw = json.loads(str(value or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("frozen business image payload is invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError("frozen business image payload is invalid")
    version = raw.get("version")
    if version not in _SUPPORTED_BUSINESS_IMAGE_BRIEF_VERSIONS:
        raise ValueError("unsupported frozen business image payload version")
    expected = {"version", "brief", "wait_seconds"}
    if version == 2:
        expected.add("intent")
    if "binding" in raw:
        expected.add("binding")
    if set(raw) != expected:
        raise ValueError("frozen business image payload is invalid")
    if version == 2:
        intent = raw.get("intent")
        if not isinstance(intent, dict) or set(intent) != {
            "prompt_compiler_version",
            "style_schema_version",
            "style",
        }:
            raise ValueError("frozen business visual intent is invalid")
        if intent.get("prompt_compiler_version") != _PROMPT_COMPILER_VERSION:
            raise ValueError("unsupported visual prompt compiler version")
        if intent.get("style_schema_version") not in SUPPORTED_STYLE_SCHEMA_VERSIONS:
            raise ValueError("unsupported visual style schema version")
        style = intent.get("style")
        if not isinstance(style, dict):
            raise ValueError("frozen business visual style is invalid")
        VisualStyleIntent.from_mapping(style)
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


def frozen_business_visual_style(value: str) -> VisualStyleIntent | None:
    """Return the exact style snapshot for v2 receipts; legacy v1 has none."""

    _load_frozen_business_visual_payload(value)
    raw = json.loads(str(value or ""))
    if int(raw.get("version") or 0) < 2:
        return None
    intent = raw.get("intent")
    if not isinstance(intent, dict):
        raise ValueError("frozen business visual intent is invalid")
    style = intent.get("style")
    if not isinstance(style, dict):
        raise ValueError("frozen business visual style is invalid")
    return VisualStyleIntent.from_mapping(style)


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
    style_intent: VisualStyleIntent | None = None,
) -> VisualCreativeBrief:
    owner_request = normalize_business_image_request(request)
    visual_kind = str(kind or "").strip().lower()
    if visual_kind not in {"image", "video"}:
        raise ValueError("business visual kind must be image or video")
    compiled = compile_visual_prompt(
        request=owner_request,
        kind=visual_kind,
        brand_context=str(brand_context or "").strip()[:1200],
        purpose="owner_visual",
        style_intent=style_intent,
    )
    return VisualCreativeBrief(
        kind=visual_kind,
        prompt=compiled.prompt,
        country_code=str(country_code or ""),
        preferred_provider=str(preferred_provider or ""),
        aspect_ratio="9:16" if visual_kind == "video" else "4:5",
        duration_seconds=8,
        negative_prompt=compiled.negative_prompt,
        brand_context=str(brand_context or "").strip()[:1200],
    )


def build_business_image_brief(
    *,
    request: str,
    brand_context: str = "",
    country_code: str = "",
    preferred_provider: str = "",
    style_intent: VisualStyleIntent | None = None,
) -> VisualCreativeBrief:
    return build_business_visual_brief(
        request=request,
        kind="image",
        brand_context=brand_context,
        country_code=country_code,
        preferred_provider=preferred_provider,
        style_intent=style_intent,
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
    style_intent: VisualStyleIntent | None = None,
) -> VisualCreativeJob:
    # Compatibility path for existing callers. Restart-safe owner generation freezes
    # the provider brief first and calls create_business_image_from_frozen_payload.
    payload = freeze_business_image_payload(
        request=request,
        brand_context=brand_context,
        country_code=country_code,
        preferred_provider=preferred_provider,
        style_intent=style_intent,
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
    style_intent: VisualStyleIntent | None = None,
) -> VisualCreativeBrief:
    visual_kind = str(kind or "image").strip().lower()
    if visual_kind not in {"image", "video"}:
        raise ValueError("kind must be image or video")
    service = normalize_business_image_request(str(title or ""))
    context = " ".join(str(body or "").replace("\x00", " ").split()).strip()
    owner_request = f"Create an advertising visual for {service}."
    if context:
        owner_request += f" Context: {context}"
    compiled = compile_visual_prompt(
        request=owner_request,
        kind=visual_kind,
        brand_context=f"Service or offering: {service}. {context}".strip()[:1200],
        purpose="advertising",
        style_intent=style_intent,
    )
    return VisualCreativeBrief(
        kind=visual_kind,
        prompt=compiled.prompt,
        country_code=str(country_code or ""),
        preferred_provider=str(preferred_provider or ""),
        aspect_ratio="4:5" if visual_kind == "image" else "9:16",
        duration_seconds=8,
        negative_prompt=compiled.negative_prompt,
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
    style_intent: VisualStyleIntent | None = None,
) -> VisualCreativeJob:
    try:
        return submit_visual(
            build_ad_visual_brief(
                title=title,
                body=body,
                kind=kind,
                country_code=country_code,
                preferred_provider=preferred_provider,
                style_intent=style_intent,
            ),
            scope_id=scope_id,
            idempotency_key=idempotency_key,
            wait_seconds=max(0, min(int(wait_seconds or 0), 60)),
        )
    except VisualCreativeGatewayError as exc:
        raise VisualCreativeError("visual_creative_generation_failed") from exc


_MAX_MATERIALIZED_IMAGE_SIDE = 8192
_MAX_MATERIALIZED_IMAGE_PIXELS = 25_000_000


def _validate_materialized_image_dimensions(image) -> None:
    width, height = image.size
    if (
        width < 64
        or height < 64
        or width > _MAX_MATERIALIZED_IMAGE_SIDE
        or height > _MAX_MATERIALIZED_IMAGE_SIDE
        or width * height > _MAX_MATERIALIZED_IMAGE_PIXELS
    ):
        raise VisualCreativeError("visual_creative_invalid_image_dimensions")


def _image_background_repair_box(image) -> tuple[int, int, int, int] | None:
    """Return a conservative crop box for pathological one-sided blank padding.

    Generative providers occasionally return a valid bitmap where most of one side
    is a nearly uniform filler/transparent band. Telegram then faithfully displays
    the broken composition. We only repair extreme cases so intentional copy space
    is preserved.
    """

    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError as exc:
        raise VisualCreativeError("visual_creative_image_runtime_unavailable") from exc

    _validate_materialized_image_dimensions(image)
    width, height = image.size

    probe = image.convert("RGB")
    probe.thumbnail((256, 256), Image.Resampling.LANCZOS)
    pw, ph = probe.size
    band_h = max(4, ph // 6)
    band_w = max(4, pw // 6)
    bands = (
        probe.crop((0, 0, pw, band_h)),
        probe.crop((0, ph - band_h, pw, ph)),
        probe.crop((0, 0, band_w, ph)),
        probe.crop((pw - band_w, 0, pw, ph)),
    )
    stats = [ImageStat.Stat(band) for band in bands]
    quietest = min(stats, key=lambda item: max(item.stddev or [999.0]))
    if max(quietest.stddev or [999.0]) > 8.0:
        return None

    background = tuple(int(round(value)) for value in quietest.mean[:3])
    diff = ImageChops.difference(probe, Image.new("RGB", probe.size, background))
    channel_max = ImageChops.lighter(diff.getchannel("R"), diff.getchannel("G"))
    channel_max = ImageChops.lighter(channel_max, diff.getchannel("B"))
    mask = channel_max.point(lambda value: 255 if value >= 18 else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return None

    left, top, right, bottom_y = bbox
    blank_bottom = (ph - bottom_y) / ph
    blank_top = top / ph
    blank_left = left / pw
    blank_right = (pw - right) / pw
    if max(blank_bottom, blank_top, blank_left, blank_right) < 0.50:
        return None

    sx = width / pw
    sy = height / ph
    crop = [
        max(0, int(left * sx)),
        max(0, int(top * sy)),
        min(width, int(right * sx)),
        min(height, int(bottom_y * sy)),
    ]
    content_w = max(1, crop[2] - crop[0])
    content_h = max(1, crop[3] - crop[1])
    margin_x = max(8, int(content_w * 0.08))
    margin_y = max(8, int(content_h * 0.08))
    crop[0] = max(0, crop[0] - margin_x)
    crop[1] = max(0, crop[1] - margin_y)
    crop[2] = min(width, crop[2] + margin_x)
    crop[3] = min(height, crop[3] + margin_y)
    if (crop[2] - crop[0]) * (crop[3] - crop[1]) >= width * height * 0.82:
        return None
    return tuple(crop)


def _normalize_materialized_image(path: Path, *, repair_blank_bands: bool = True) -> Path:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise VisualCreativeError("visual_creative_image_runtime_unavailable") from exc

    temporary = path.with_suffix(path.suffix + ".normalized.tmp")
    try:
        with Image.open(path) as opened:
            _validate_materialized_image_dimensions(opened)
            opened.verify()
        with Image.open(path) as opened:
            _validate_materialized_image_dimensions(opened)
            source_format = str(opened.format or "").upper()
            image = ImageOps.exif_transpose(opened)
            _validate_materialized_image_dimensions(image)
            image.load()
            if "A" not in image.getbands() and "transparency" in image.info:
                image = image.convert("RGBA")

            alpha = image.getchannel("A") if "A" in image.getbands() else None
            alpha_box = alpha.point(lambda value: 255 if value >= 16 else 0).getbbox() if alpha is not None else None
            if alpha is not None and alpha_box is None:
                raise VisualCreativeError("visual_creative_invalid_image_asset")
            if alpha_box is not None and alpha_box != (0, 0, image.width, image.height):
                visible = image.crop(alpha_box)
                if visible.width * visible.height < image.width * image.height * 0.82:
                    image = visible

            if "A" in image.getbands():
                flattened = Image.new("RGB", image.size, (255, 255, 255))
                flattened.paste(image, mask=image.getchannel("A"))
                image = flattened
            else:
                image = image.convert("RGB")

            repair_box = _image_background_repair_box(image) if repair_blank_bands else None
            if repair_box is not None:
                image = image.crop(repair_box)

            if image.width < 64 or image.height < 64:
                raise VisualCreativeError("visual_creative_invalid_image_dimensions")

            suffix = path.suffix.lower()
            if suffix in {".jpg", ".jpeg"} or source_format in {"JPG", "JPEG"}:
                image.save(temporary, format="JPEG", quality=94, optimize=True)
            elif suffix == ".webp" or source_format == "WEBP":
                image.save(temporary, format="WEBP", quality=94, method=6)
            else:
                image.save(temporary, format="PNG", optimize=True)
        os.replace(temporary, path)
        return path
    except Image.DecompressionBombError as exc:
        temporary.unlink(missing_ok=True)
        raise VisualCreativeError("visual_creative_invalid_image_asset") from exc
    except UnidentifiedImageError as exc:
        temporary.unlink(missing_ok=True)
        raise VisualCreativeError("visual_creative_invalid_image_asset") from exc
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise VisualCreativeError("visual_creative_invalid_image_asset") from exc
    except ValueError as exc:
        temporary.unlink(missing_ok=True)
        raise VisualCreativeError("visual_creative_invalid_image_asset") from exc


def materialize_ad_visual(
    job: VisualCreativeJob,
    *,
    output_dir: str | None = None,
    repair_blank_bands: bool = True,
) -> Path:
    try:
        path = download_visual(job, output_dir=output_dir)
        if str(job.kind or "").strip().lower() == "image":
            return _normalize_materialized_image(
                path,
                repair_blank_bands=repair_blank_bands,
            )
        return path
    except (VisualCreativeGatewayError, OSError) as exc:
        raise VisualCreativeError("visual_creative_materialization_failed") from exc


def poll_ad_visual(*, job_id: str, scope_id: str) -> VisualCreativeJob:
    try:
        return poll_visual(job_id, scope_id=scope_id)
    except VisualCreativeGatewayError as exc:
        raise VisualCreativeError("visual_creative_poll_failed") from exc


def wait_ad_visual(
    job: VisualCreativeJob,
    *,
    wait_seconds: int = 60,
    poll_interval: float = 2.0,
) -> VisualCreativeJob:
    """Wait for the exact existing visual job without creating a second paid job."""

    try:
        return wait_visual(
            job,
            wait_seconds=wait_seconds,
            poll_interval=poll_interval,
        )
    except VisualCreativeGatewayError as exc:
        raise VisualCreativeError("visual_creative_poll_failed") from exc


__all__ = [
    "VisualCreativeError",
    "visual_generation_ready",
    "visual_video_generation_mode",
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
    "frozen_business_visual_style",
    "normalize_business_image_request",
    "build_ad_visual_brief",
    "create_ad_visual",
    "materialize_ad_visual",
    "poll_ad_visual",
    "wait_ad_visual",
]