from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services import visual_creative_gateway as visual_gateway
from services.visual_creative_gateway import VisualCreativeGatewayError

_RENDER_PACK_FORMATS = frozenset({"square", "feed", "story", "landscape"})


@dataclass(frozen=True, slots=True)
class VisualGatewayContract:
    contract_version: str
    capabilities: frozenset[str]
    render_formats: frozenset[str]


def _tokens(value: object, *, maximum: int = 32) -> frozenset[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise VisualCreativeGatewayError("visual_gateway_invalid_capabilities")
    out: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise VisualCreativeGatewayError("visual_gateway_invalid_capabilities")
        token = item.strip().lower()
        if not token or len(token) > 64 or not token.replace("_", "").isalnum():
            raise VisualCreativeGatewayError("visual_gateway_invalid_capabilities")
        out.add(token)
    return frozenset(out)


def get_gateway_contract() -> VisualGatewayContract:
    """Read the authenticated gateway contract through the canonical transport.

    This endpoint is deliberately checked before any paid generation request.  A
    missing/old endpoint therefore fails closed rather than spending provider
    money and discovering an incompatible render contract afterwards.
    """

    try:
        payload: dict[str, Any] = visual_gateway._json(
            "GET",
            "/v1/capabilities",
            timeout_seconds=10,
        )
    except VisualCreativeGatewayError as exc:
        if str(exc) == "visual_gateway_http_404":
            raise VisualCreativeGatewayError(
                "visual_gateway_render_pack_unavailable"
            ) from None
        raise

    capabilities = _tokens(payload.get("capabilities"))
    render_formats = _tokens(payload.get("render_formats"))
    contract_version = str(payload.get("contract_version") or "").strip()[:32]
    return VisualGatewayContract(
        contract_version=contract_version,
        capabilities=capabilities,
        render_formats=render_formats,
    )


def require_render_pack_contract(*, formats: tuple[str, ...]) -> VisualGatewayContract:
    requested = frozenset(str(item or "").strip().lower() for item in formats)
    if not requested or not requested.issubset(_RENDER_PACK_FORMATS):
        raise ValueError("invalid_creative_render_formats")

    contract = get_gateway_contract()
    if "render_pack" not in contract.capabilities:
        raise VisualCreativeGatewayError("visual_gateway_render_pack_unavailable")
    if not requested.issubset(contract.render_formats):
        raise VisualCreativeGatewayError("visual_gateway_render_format_unavailable")
    return contract


__all__ = [
    "VisualGatewayContract",
    "get_gateway_contract",
    "require_render_pack_contract",
]
