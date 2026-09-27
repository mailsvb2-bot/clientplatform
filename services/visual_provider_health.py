from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services import visual_creative_gateway as visual_gateway
from services.visual_creative_gateway import VisualCreativeGatewayError


@dataclass(frozen=True, slots=True)
class VisualProviderHealthSnapshot:
    available: bool
    configured_image: tuple[str, ...] = ()
    configured_video: tuple[str, ...] = ()
    image_order: tuple[str, ...] = ()
    video_order: tuple[str, ...] = ()
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""


def _tuple_of_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def get_visual_provider_health_snapshot() -> VisualProviderHealthSnapshot:
    gateway = visual_gateway.gateway_snapshot()
    if not gateway.get("configured") or not gateway.get("token_configured"):
        return VisualProviderHealthSnapshot(
            available=False,
            error_code="visual_gateway_not_configured",
        )
    try:
        payload = visual_gateway._json("GET", "/v1/providers", timeout_seconds=10)
    except VisualCreativeGatewayError as exc:
        return VisualProviderHealthSnapshot(
            available=False,
            error_code=str(exc) or "visual_provider_health_unavailable",
        )

    models_raw = payload.get("models")
    runtime_raw = payload.get("runtime")
    return VisualProviderHealthSnapshot(
        available=True,
        configured_image=_tuple_of_strings(payload.get("configured_image")),
        configured_video=_tuple_of_strings(payload.get("configured_video")),
        image_order=_tuple_of_strings(payload.get("image_order")),
        video_order=_tuple_of_strings(payload.get("video_order")),
        models=models_raw if isinstance(models_raw, dict) else {},
        runtime=runtime_raw if isinstance(runtime_raw, dict) else {},
    )


__all__ = ["VisualProviderHealthSnapshot", "get_visual_provider_health_snapshot"]
