from __future__ import annotations

import asyncio
import importlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from .config import GatewayConfigurationError, SidecarConfig
from .contract import (
    CALL_REQUEST_TYPES,
    INTEGRATION_REQUEST_TYPES,
    SERVICE_CREDENTIAL_ID_METADATA_KEY,
    SERVICE_CREDENTIAL_SECRET_METADATA_KEY,
    UCR_CALL_SERVICE,
    UCR_INTEGRATION_SERVICE,
    UCR_UNIVERSAL_CONFERENCE_SERVICE,
    UNIVERSAL_CONFERENCE_REQUEST_TYPES,
    GatewayUpstreamError,
    canonical_error_to_gateway_error,
)


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
            universal_conference_pb2 = importlib.import_module(
                "ucr.v1.universal_conference_pb2"
            )
            universal_conference_pb2_grpc = importlib.import_module(
                "ucr.v1.universal_conference_pb2_grpc"
            )
        except ModuleNotFoundError as exc:
            raise GatewayConfigurationError(
                "generated pinned UCR gRPC bindings are unavailable"
            ) from exc

        self._grpc = grpc
        self._json_format = json_format
        self._integration_pb2 = integration_pb2
        self._call_pb2 = call_pb2
        self._universal_conference_pb2 = universal_conference_pb2
        if config.grpc_tls_enabled:
            root_certificates = None
            if config.grpc_root_ca_file:
                root_certificates = Path(config.grpc_root_ca_file).read_bytes()
            credentials = grpc.ssl_channel_credentials(
                root_certificates=root_certificates
            )
            channel = grpc.aio.secure_channel(config.grpc_target, credentials)
        else:
            channel = grpc.aio.insecure_channel(config.grpc_target)
        self._channel = channel
        self._integration_stub = integration_pb2_grpc.IntegrationServiceStub(channel)
        self._call_stub = call_pb2_grpc.CallServiceStub(channel)
        self._universal_conference_stub = (
            universal_conference_pb2_grpc.UniversalConferenceServiceStub(channel)
        )
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
            request_type_name = INTEGRATION_REQUEST_TYPES.get(method)
            pb2_module = self._integration_pb2
            stub = self._integration_stub
        elif service == UCR_CALL_SERVICE:
            request_type_name = CALL_REQUEST_TYPES.get(method)
            pb2_module = self._call_pb2
            stub = self._call_stub
        elif service == UCR_UNIVERSAL_CONFERENCE_SERVICE:
            request_type_name = UNIVERSAL_CONFERENCE_REQUEST_TYPES.get(method)
            pb2_module = self._universal_conference_pb2
            stub = self._universal_conference_stub
        else:
            raise GatewayUpstreamError(422, "ucr_rpc_service_not_allowed")
        if request_type_name is None:
            raise GatewayUpstreamError(422, "ucr_rpc_method_not_allowed")

        request_type = getattr(pb2_module, request_type_name)
        request_message = request_type()
        try:
            self._json_format.ParseDict(
                dict(request), request_message, ignore_unknown_fields=False
            )
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
            raise map_grpc_error(self._grpc, exc) from exc

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
            raise canonical_error_to_gateway_error(canonical_error)
        return result

    async def close(self) -> None:
        await self._channel.close()


def map_grpc_error(grpc: Any, exc: Any) -> GatewayUpstreamError:
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
