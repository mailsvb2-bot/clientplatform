from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import importlib
import ipaddress
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

UCR_PINNED_REVISION = "8097b41e69634c944c225f7071e80b991d4ddc02"
UCR_INTEGRATION_SERVICE = "ucr.v1.IntegrationService"
UCR_CALL_SERVICE = "ucr.v1.CallService"
SERVICE_CREDENTIAL_ID_METADATA_KEY = "ucr-service-credential-id-bin"
SERVICE_CREDENTIAL_SECRET_METADATA_KEY = "ucr-service-credential-secret-bin"

_MAX_HTTP_BODY_BYTES = 64 * 1024
_MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
_DEFAULT_GRPC_TIMEOUT_SECONDS = 5.0
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_HOST_PORT_RE = re.compile(r"^\[([^\]]+)\]:(\d{1,5})$|^([^:]+):(\d{1,5})$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

_INTEGRATION_REQUEST_TYPES = {
    "SubmitCommand": "IntegrationCommandRequest",
    "CreateIdentity": "IntegrationCreateIdentityRequest",
    "LinkIdentity": "IntegrationLinkIdentityRequest",
    "GetIdentity": "IntegrationGetIdentityRequest",
    "ResolveIdentityBinding": "IntegrationResolveIdentityBindingRequest",
    "CreateConversation": "IntegrationCreateConversationRequest",
    "GetConversation": "IntegrationGetConversationRequest",
    "SendMessage": "IntegrationSendMessageRequest",
    "GetMessage": "IntegrationGetMessageRequest",
    "CreateCommunicationIntent": "IntegrationCreateCommunicationIntentRequest",
    "GetCommunicationIntent": "IntegrationGetCommunicationIntentRequest",
}
_CALL_REQUEST_TYPES = {
    "StartCall": "CallStartRequest",
    "GetCall": "CallGetRequest",
    "SignalCall": "CallSignalRequest",
}
_MUTATING_METHODS = frozenset(
    {
        (UCR_INTEGRATION_SERVICE, "SubmitCommand"),
        (UCR_INTEGRATION_SERVICE, "CreateIdentity"),
        (UCR_INTEGRATION_SERVICE, "LinkIdentity"),
        (UCR_INTEGRATION_SERVICE, "CreateConversation"),
        (UCR_INTEGRATION_SERVICE, "SendMessage"),
        (UCR_INTEGRATION_SERVICE, "CreateCommunicationIntent"),
        (UCR_CALL_SERVICE, "StartCall"),
        (UCR_CALL_SERVICE, "SignalCall"),
    }
)
_UCR_CANONICAL_ERROR_MAP = {
    "ERROR_CODE_INVALID_ARGUMENT": (422, "ucr_error_invalid_argument"),
    "ERROR_CODE_MALFORMED_FRAME": (422, "ucr_error_malformed_frame"),
    "ERROR_CODE_UNSUPPORTED_PROTOCOL_VERSION": (422, "ucr_error_unsupported_protocol_version"),
    "ERROR_CODE_DOWNGRADE_REJECTED": (422, "ucr_error_downgrade_rejected"),
    "ERROR_CODE_UNSUPPORTED_CRITICAL_EXTENSION": (422, "ucr_error_unsupported_critical_extension"),
    "ERROR_CODE_CAPABILITY_MISMATCH": (422, "ucr_error_capability_mismatch"),
    "ERROR_CODE_UNAUTHENTICATED": (403, "ucr_error_unauthenticated"),
    "ERROR_CODE_PERMISSION_DENIED": (403, "ucr_error_permission_denied"),
    "ERROR_CODE_POLICY_DENIED": (403, "ucr_error_policy_denied"),
    "ERROR_CODE_RATE_LIMITED": (429, "ucr_error_rate_limited"),
    "ERROR_CODE_RESOURCE_EXHAUSTED": (429, "ucr_error_resource_exhausted"),
    "ERROR_CODE_DEADLINE_EXCEEDED": (503, "ucr_error_deadline_exceeded"),
    "ERROR_CODE_CANCELLED": (503, "ucr_error_cancelled"),
    "ERROR_CODE_TEMPORARILY_UNAVAILABLE": (503, "ucr_error_temporarily_unavailable"),
    "ERROR_CODE_INTEGRITY_FAILURE": (422, "ucr_error_integrity_failure"),
    "ERROR_CODE_CONFLICT": (409, "ucr_error_conflict"),
    "ERROR_CODE_NOT_FOUND": (422, "ucr_error_not_found"),
    "ERROR_CODE_INTERNAL": (502, "ucr_error_internal"),
}


class GatewayConfigurationError(RuntimeError):
    pass


class GatewayUpstreamError(RuntimeError):
    def __init__(self, status_code: int, error_code: str) -> None:
        super().__init__(error_code)
        self.status_code = int(status_code)
        self.error_code = str(error_code)


class UcrRpcBackend(Protocol):
    async def health(self) -> None: ...

    async def invoke(
        self,
        *,
        service: str,
        method: str,
        request: Mapping[str, Any],
        idempotency_key: str | None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SidecarConfig:
    client_token: str = field(repr=False)
    grpc_target: str
    grpc_tls_enabled: bool
    service_credential_id: bytes = field(repr=False)
    service_credential_secret: bytes = field(repr=False)
    grpc_timeout_seconds: float = _DEFAULT_GRPC_TIMEOUT_SECONDS
    grpc_root_ca_file: str = ""
    bind_host: str = "127.0.0.1"
    bind_port: int = 8098

    def __post_init__(self) -> None:
        if len(self.client_token.encode("utf-8")) < 32:
            raise GatewayConfigurationError("gateway client token must be at least 32 bytes")
        if not self.service_credential_id or len(self.service_credential_id) > 128:
            raise GatewayConfigurationError("UCR service credential id is invalid")
        if len(self.service_credential_secret) != 32:
            raise GatewayConfigurationError("UCR service credential secret must be exactly 32 bytes")
        host, port = _split_host_port(self.grpc_target)
        if not 1 <= port <= 65535:
            raise GatewayConfigurationError("UCR gRPC target port is invalid")
        if not self.grpc_tls_enabled and not _is_loopback_host(host):
            raise GatewayConfigurationError("UCR gRPC TLS is required outside loopback")
        if not 0.25 <= float(self.grpc_timeout_seconds) <= 30.0:
            raise GatewayConfigurationError("UCR gRPC timeout must be 0.25..30 seconds")
        if not 1 <= int(self.bind_port) <= 65535:
            raise GatewayConfigurationError("gateway bind port is invalid")
        if self.grpc_root_ca_file and not self.grpc_tls_enabled:
            raise GatewayConfigurationError("UCR root CA requires TLS")


class GrpcUcrBackend:
    """Transport-only adapter over the pinned public UCR protobuf services."""

    def __init__(self, config: SidecarConfig) -> None:
        self._config = config
        try:
            grpc = importlib.import_module("grpc")
            json_format = importlib.import_module("google.protobuf.json_format")
            integration_pb2 = importlib.import_module("ucr.v1.integration_pb2")
            integration_pb2_grpc = importlib.import_module("ucr.v1.integration_pb2_grpc")
            call_pb2 = importlib.import_module("ucr.v1.call_pb2")
            call_pb2_grpc = importlib.import_module("ucr.v1.call_pb2_grpc")
        except ModuleNotFoundError as exc:
            raise GatewayConfigurationError("generated pinned UCR gRPC bindings are unavailable") from exc

        self._grpc = grpc
        self._json_format = json_format
        self._integration_pb2 = integration_pb2
        self._call_pb2 = call_pb2
        if config.grpc_tls_enabled:
            root_certificates = None
            if config.grpc_root_ca_file:
                root_certificates = Path(config.grpc_root_ca_file).read_bytes()
            credentials = grpc.ssl_channel_credentials(root_certificates=root_certificates)
            channel = grpc.aio.secure_channel(config.grpc_target, credentials)
        else:
            channel = grpc.aio.insecure_channel(config.grpc_target)
        self._channel = channel
        self._integration_stub = integration_pb2_grpc.IntegrationServiceStub(channel)
        self._call_stub = call_pb2_grpc.CallServiceStub(channel)
        self._metadata = (
            (SERVICE_CREDENTIAL_ID_METADATA_KEY, config.service_credential_id),
            (SERVICE_CREDENTIAL_SECRET_METADATA_KEY, config.service_credential_secret),
        )

    async def health(self) -> None:
        try:
            await asyncio.wait_for(
                self._channel.channel_ready(), timeout=self._config.grpc_timeout_seconds
            )
        except (asyncio.TimeoutError, OSError) as exc:
            raise GatewayUpstreamError(503, "ucr_grpc_unavailable") from exc

    async def invoke(
        self,
        *,
        service: str,
        method: str,
        request: Mapping[str, Any],
        idempotency_key: str | None,
    ) -> Mapping[str, Any]:
        del idempotency_key  # Canonical UCR IDs remain the durable dedupe owner.
        if service == UCR_INTEGRATION_SERVICE:
            request_type_name = _INTEGRATION_REQUEST_TYPES.get(method)
            pb2_module = self._integration_pb2
            stub = self._integration_stub
        elif service == UCR_CALL_SERVICE:
            request_type_name = _CALL_REQUEST_TYPES.get(method)
            pb2_module = self._call_pb2
            stub = self._call_stub
        else:
            raise GatewayUpstreamError(422, "ucr_rpc_service_not_allowed")
        if request_type_name is None:
            raise GatewayUpstreamError(422, "ucr_rpc_method_not_allowed")

        request_type = getattr(pb2_module, request_type_name)
        request_message = request_type()
        try:
            self._json_format.ParseDict(dict(request), request_message, ignore_unknown_fields=False)
        except (TypeError, ValueError) as exc:
            raise GatewayUpstreamError(422, "ucr_rpc_request_invalid") from exc

        rpc = getattr(stub, method)
        try:
            response_message = await rpc(
                request_message,
                timeout=self._config.grpc_timeout_seconds,
                metadata=self._metadata,
            )
        except self._grpc.aio.AioRpcError as exc:
            raise _map_grpc_error(self._grpc, exc) from exc

        result = self._json_format.MessageToDict(
            response_message,
            preserving_proto_field_name=False,
            use_integers_for_enums=False,
        )
        if not isinstance(result, dict):
            raise GatewayUpstreamError(502, "ucr_grpc_response_invalid")
        canonical_error = result.get("error")
        if canonical_error is not None:
            if not isinstance(canonical_error, Mapping):
                raise GatewayUpstreamError(502, "ucr_grpc_response_invalid")
            raise _canonical_error_to_gateway_error(canonical_error)
        return result

    async def close(self) -> None:
        await self._channel.close()


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
        if len(body) > _MAX_HTTP_BODY_BYTES:
            return 413, {"ok": False, "error": "ucr_gateway_request_too_large"}
        try:
            envelope = _decode_json_object(body)
            service, method, request_payload, idempotency_key = _validate_envelope(
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
            encoded = _encode_json(payload)
        except (TypeError, ValueError):
            return 502, {"ok": False, "error": "ucr_grpc_response_invalid"}
        if len(encoded) > _MAX_HTTP_RESPONSE_BYTES:
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


def load_config(environment: Mapping[str, str] | None = None) -> SidecarConfig:
    env = environment if environment is not None else os.environ
    client_token = str(env.get("CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN") or "").strip()
    grpc_target = str(env.get("CLIENTPLATFORM_UCR_GRPC_TARGET") or "").strip()
    if not grpc_target:
        raise GatewayConfigurationError("CLIENTPLATFORM_UCR_GRPC_TARGET is required")
    tls_enabled = (
        str(env.get("CLIENTPLATFORM_UCR_GRPC_TLS_ENABLED") or "1").strip().lower()
        in _TRUE_VALUES
    )
    credential_id = str(
        env.get("CLIENTPLATFORM_UCR_SERVICE_CREDENTIAL_ID") or ""
    ).strip().encode("utf-8")
    secret_b64 = str(
        env.get("CLIENTPLATFORM_SECRET_UCR_SERVICE_CREDENTIAL_B64") or ""
    ).strip()
    try:
        credential_secret = base64.b64decode(secret_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise GatewayConfigurationError(
            "UCR service credential secret must be valid base64"
        ) from exc
    raw_timeout = str(
        env.get("CLIENTPLATFORM_UCR_GRPC_TIMEOUT_SEC") or _DEFAULT_GRPC_TIMEOUT_SECONDS
    ).strip()
    raw_port = str(env.get("CLIENTPLATFORM_UCR_GATEWAY_BIND_PORT") or "8098").strip()
    try:
        timeout_seconds = float(raw_timeout)
        bind_port = int(raw_port)
    except ValueError as exc:
        raise GatewayConfigurationError("gateway numeric configuration is invalid") from exc
    return SidecarConfig(
        client_token=client_token,
        grpc_target=grpc_target,
        grpc_tls_enabled=tls_enabled,
        service_credential_id=credential_id,
        service_credential_secret=credential_secret,
        grpc_timeout_seconds=timeout_seconds,
        grpc_root_ca_file=str(
            env.get("CLIENTPLATFORM_UCR_GRPC_ROOT_CA_FILE") or ""
        ).strip(),
        bind_host=str(
            env.get("CLIENTPLATFORM_UCR_GATEWAY_BIND_HOST") or "127.0.0.1"
        ).strip(),
        bind_port=bind_port,
    )


def _decode_json_object(body: bytes) -> dict[str, Any]:
    if not body:
        raise ValueError("ucr_gateway_request_empty")

    def reject_constant(_value: str) -> None:
        raise ValueError("ucr_gateway_request_nonfinite")

    try:
        decoded = json.loads(body.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("ucr_gateway_request_invalid_json") from exc
    if not isinstance(decoded, dict):
        raise ValueError("ucr_gateway_request_not_object")
    _assert_finite_json(decoded)
    return decoded


def _validate_envelope(
    envelope: Mapping[str, Any], headers: Mapping[str, str]
) -> tuple[str, str, dict[str, Any], str | None]:
    if envelope.get("version") != 1:
        raise ValueError("ucr_gateway_version_invalid")
    service = str(envelope.get("service") or "").strip()
    method = str(envelope.get("method") or "").strip()
    request = envelope.get("request")
    if service == UCR_INTEGRATION_SERVICE:
        allowed = _INTEGRATION_REQUEST_TYPES
    elif service == UCR_CALL_SERVICE:
        allowed = _CALL_REQUEST_TYPES
    else:
        raise ValueError("ucr_rpc_service_not_allowed")
    if method not in allowed:
        raise ValueError("ucr_rpc_method_not_allowed")
    if not isinstance(request, dict) or not request:
        raise ValueError("ucr_rpc_request_invalid")
    _assert_finite_json(request)

    raw_key = str(headers.get("Idempotency-Key") or "").strip()
    is_mutation = (service, method) in _MUTATING_METHODS
    if is_mutation:
        if not _IDEMPOTENCY_KEY_RE.fullmatch(raw_key):
            raise ValueError("ucr_gateway_idempotency_key_invalid")
        idempotency_key: str | None = raw_key
    else:
        if raw_key:
            raise ValueError("ucr_gateway_read_idempotency_forbidden")
        idempotency_key = None
    return service, method, dict(request), idempotency_key


def _assert_finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("ucr_gateway_request_nonfinite")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("ucr_gateway_request_key_invalid")
            _assert_finite_json(child)
    elif isinstance(value, list):
        for child in value:
            _assert_finite_json(child)


def _encode_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _split_host_port(value: str) -> tuple[str, int]:
    match = _HOST_PORT_RE.fullmatch(str(value or "").strip())
    if not match:
        raise GatewayConfigurationError("UCR gRPC target must be host:port")
    host = match.group(1) or match.group(3) or ""
    port = int(match.group(2) or match.group(4) or "0")
    if not host:
        raise GatewayConfigurationError("UCR gRPC target host is invalid")
    return host, port


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _canonical_error_to_gateway_error(error: Mapping[str, Any]) -> GatewayUpstreamError:
    code = str(error.get("code") or "ERROR_CODE_UNSPECIFIED").strip()
    status_code, error_code = _UCR_CANONICAL_ERROR_MAP.get(
        code, (502, "ucr_error_unspecified")
    )
    return GatewayUpstreamError(status_code, error_code)


def _map_grpc_error(grpc: Any, exc: Any) -> GatewayUpstreamError:
    code = exc.code()
    mapping = {
        grpc.StatusCode.UNAUTHENTICATED: (403, "ucr_grpc_unauthenticated"),
        grpc.StatusCode.PERMISSION_DENIED: (403, "ucr_grpc_permission_denied"),
        grpc.StatusCode.INVALID_ARGUMENT: (422, "ucr_grpc_invalid_argument"),
        grpc.StatusCode.FAILED_PRECONDITION: (422, "ucr_grpc_failed_precondition"),
        grpc.StatusCode.ALREADY_EXISTS: (409, "ucr_grpc_conflict"),
        grpc.StatusCode.ABORTED: (409, "ucr_grpc_conflict"),
        grpc.StatusCode.RESOURCE_EXHAUSTED: (429, "ucr_grpc_resource_exhausted"),
        grpc.StatusCode.DEADLINE_EXCEEDED: (503, "ucr_grpc_unavailable"),
        grpc.StatusCode.UNAVAILABLE: (503, "ucr_grpc_unavailable"),
        grpc.StatusCode.NOT_FOUND: (422, "ucr_grpc_not_found"),
    }
    status_code, error_code = mapping.get(code, (502, "ucr_grpc_failure"))
    return GatewayUpstreamError(status_code, error_code)


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
    app = web.Application(client_max_size=_MAX_HTTP_BODY_BYTES)

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
