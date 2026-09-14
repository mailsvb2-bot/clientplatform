from __future__ import annotations

import asyncio
import hmac
import json
from typing import Any, Mapping

from .config import GatewayConfigurationError, SidecarConfig, load_config
from .contract import (
    MAX_HTTP_BODY_BYTES,
    MAX_HTTP_RESPONSE_BYTES,
    UCR_CALL_SERVICE,
    UCR_INTEGRATION_SERVICE,
    UCR_PINNED_REVISION,
    GatewayUpstreamError,
    canonical_error_to_gateway_error,
    decode_json_object,
    encode_json,
    validate_envelope,
)
from .grpc_backend import GrpcUcrBackend, UcrRpcBackend

# Compatibility for the focused regression tests and callers of the initial sidecar draft.
_canonical_error_to_gateway_error = canonical_error_to_gateway_error


class GatewayService:
    def __init__(self, *, config: SidecarConfig, backend: UcrRpcBackend) -> None:
        self._config = config
        self._backend = backend

    async def health(self, *, headers: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        auth_error = self._authorize(headers)
        if auth_error is not None:
            return auth_error
        try:
            await self._backend.health()
        except GatewayUpstreamError as exc:
            return exc.status_code, {"ok": False, "error": exc.error_code}
        return 200, {"ok": True, "ucr_revision": UCR_PINNED_REVISION}

    async def rpc(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, Any]]:
        auth_error = self._authorize(headers)
        if auth_error is not None:
            return auth_error
        if len(body) > MAX_HTTP_BODY_BYTES:
            return 413, {"ok": False, "error": "ucr_gateway_request_too_large"}
        try:
            envelope = decode_json_object(body)
            service, method, request_payload, idempotency_key = validate_envelope(
                envelope, headers
            )
        except ValueError as exc:
            return 422, {"ok": False, "error": str(exc)}

        try:
            result = await self._backend.invoke(
                service=service,
                method=method,
                request=request_payload,
                idempotency_key=idempotency_key,
            )
        except GatewayUpstreamError as exc:
            return exc.status_code, {"ok": False, "error": exc.error_code}

        payload = {
            "ok": True,
            "ucr_revision": UCR_PINNED_REVISION,
            "service": service,
            "method": method,
            "result": dict(result),
        }
        try:
            encoded = encode_json(payload)
        except (TypeError, ValueError):
            return 502, {"ok": False, "error": "ucr_grpc_response_invalid"}
        if len(encoded) > MAX_HTTP_RESPONSE_BYTES:
            return 502, {"ok": False, "error": "ucr_grpc_response_too_large"}
        return 200, payload

    def _authorize(self, headers: Mapping[str, str]) -> tuple[int, dict[str, Any]] | None:
        authorization = str(headers.get("Authorization") or "")
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token:
            return 401, {"ok": False, "error": "ucr_gateway_unauthorized"}
        if not hmac.compare_digest(
            token.encode("utf-8"), self._config.client_token.encode("utf-8")
        ):
            return 401, {"ok": False, "error": "ucr_gateway_unauthorized"}
        presented_revision = str(headers.get("X-ClientPlatform-UCR-Revision") or "").strip()
        if presented_revision != UCR_PINNED_REVISION:
            return 409, {"ok": False, "error": "ucr_gateway_revision_mismatch"}
        return None


def create_application(
    *, config: SidecarConfig | None = None, backend: UcrRpcBackend | None = None
) -> Any:
    try:
        from aiohttp import web
    except ModuleNotFoundError as exc:
        raise GatewayConfigurationError(
            "aiohttp is required for the UCR gateway sidecar"
        ) from exc

    resolved_config = config or load_config()
    resolved_backend = backend or GrpcUcrBackend(resolved_config)
    service = GatewayService(config=resolved_config, backend=resolved_backend)
    app = web.Application(client_max_size=MAX_HTTP_BODY_BYTES)

    async def health_handler(request: Any) -> Any:
        status, payload = await service.health(headers=request.headers)
        return web.json_response(payload, status=status, dumps=_json_dumps)

    async def rpc_handler(request: Any) -> Any:
        body = await request.read()
        status, payload = await service.rpc(headers=request.headers, body=body)
        return web.json_response(payload, status=status, dumps=_json_dumps)

    async def close_backend(_app: Any) -> None:
        close = getattr(resolved_backend, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    app.router.add_get("/v1/health", health_handler)
    app.router.add_post("/v1/rpc", rpc_handler)
    app.on_cleanup.append(close_backend)
    return app


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def main() -> None:
    try:
        from aiohttp import web
    except ModuleNotFoundError as exc:
        raise GatewayConfigurationError(
            "aiohttp is required for the UCR gateway sidecar"
        ) from exc
    config = load_config()
    app = create_application(config=config)
    web.run_app(app, host=config.bind_host, port=config.bind_port, print=None)


if __name__ == "__main__":
    main()


__all__ = [
    "UCR_PINNED_REVISION",
    "UCR_INTEGRATION_SERVICE",
    "UCR_CALL_SERVICE",
    "GatewayConfigurationError",
    "GatewayService",
    "GatewayUpstreamError",
    "SidecarConfig",
    "create_application",
    "load_config",
]
