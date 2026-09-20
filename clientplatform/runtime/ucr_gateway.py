from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit, urlunsplit

from clientplatform.runtime.secrets import (
    CredentialProvider,
    EnvironmentCredentialProvider,
    SecretReferenceError,
)


UCR_PINNED_REVISION = "22d598e057769d59fe0aa47e169cf2eb904abb18"
UCR_INTEGRATION_SERVICE = "ucr.v1.IntegrationService"
UCR_CALL_SERVICE = "ucr.v1.CallService"
UCR_UNIVERSAL_CONFERENCE_SERVICE = "ucr.v1.UniversalConferenceService"
_DEFAULT_TOKEN_REFERENCE = "secret://env/CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN"
_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_MAX_RESPONSE_BYTES = 128 * 1024
_MAX_REQUEST_BYTES = 64 * 1024
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_RPC_PATH = "/v1/rpc"
_EnumT = TypeVar("_EnumT", bound=Enum)


class UcrIntegrationMethod(str, Enum):
    SUBMIT_COMMAND = "SubmitCommand"
    CREATE_IDENTITY = "CreateIdentity"
    LINK_IDENTITY = "LinkIdentity"
    GET_IDENTITY = "GetIdentity"
    RESOLVE_IDENTITY_BINDING = "ResolveIdentityBinding"
    CREATE_CONVERSATION = "CreateConversation"
    GET_CONVERSATION = "GetConversation"
    SEND_MESSAGE = "SendMessage"
    GET_MESSAGE = "GetMessage"
    CREATE_COMMUNICATION_INTENT = "CreateCommunicationIntent"
    GET_COMMUNICATION_INTENT = "GetCommunicationIntent"


class UcrCallMethod(str, Enum):
    START_CALL = "StartCall"
    GET_CALL = "GetCall"
    SIGNAL_CALL = "SignalCall"


class UcrUniversalConferenceMethod(str, Enum):
    GET_PARTICIPANT_ATTENDANCE = "GetParticipantAttendance"
    GET_CAPABILITIES = "GetCapabilities"


_INTEGRATION_MUTATIONS = frozenset(
    {
        UcrIntegrationMethod.SUBMIT_COMMAND,
        UcrIntegrationMethod.CREATE_IDENTITY,
        UcrIntegrationMethod.LINK_IDENTITY,
        UcrIntegrationMethod.CREATE_CONVERSATION,
        UcrIntegrationMethod.SEND_MESSAGE,
        UcrIntegrationMethod.CREATE_COMMUNICATION_INTENT,
    }
)
_CALL_MUTATIONS = frozenset({UcrCallMethod.START_CALL, UcrCallMethod.SIGNAL_CALL})


class UcrGatewayError(RuntimeError):
    """Base error for the optional Universal Communication Runtime boundary."""


class UcrGatewayConfigurationError(UcrGatewayError):
    """The gateway is disabled, incomplete, or unsafe to call."""


class UcrGatewayUnavailable(UcrGatewayError):
    """The configured gateway could not be reached or asked the caller to retry."""


class UcrGatewayRejected(UcrGatewayError):
    """The gateway rejected an authenticated, well-formed ClientPlatform request."""


class UcrGatewayProtocolError(UcrGatewayError):
    """The gateway response violated the pinned ClientPlatform/UCR contract."""


@dataclass(frozen=True, slots=True)
class UcrGatewayConfig:
    enabled: bool
    base_url: str
    token_reference: str
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES
    pinned_revision: str = UCR_PINNED_REVISION

    def __post_init__(self) -> None:
        if self.pinned_revision != UCR_PINNED_REVISION:
            raise UcrGatewayConfigurationError("UCR revision must use the repository pin")
        if not 0.25 <= float(self.timeout_seconds) <= 30.0:
            raise UcrGatewayConfigurationError("UCR gateway timeout must be 0.25..30 seconds")
        if not 1024 <= int(self.max_response_bytes) <= 1024 * 1024:
            raise UcrGatewayConfigurationError("UCR gateway response limit is invalid")
        if not _is_secret_reference(self.token_reference):
            raise UcrGatewayConfigurationError("UCR gateway token must be a secret reference")
        if self.enabled and not self.base_url:
            raise UcrGatewayConfigurationError("UCR gateway URL is required when enabled")
        if self.base_url:
            object.__setattr__(self, "base_url", _normalize_base_url(self.base_url))


class UcrGatewayTransport(Protocol):
    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, Mapping[str, str], bytes]: ...


class AiohttpUcrGatewayTransport:
    """Small HTTP transport with finite time and response budgets.

    aiohttp is imported lazily so architecture and contract checks that only import the
    optional UCR boundary do not acquire a full application-runtime dependency.
    """

    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, Mapping[str, str], bytes]:
        try:
            import aiohttp
        except ModuleNotFoundError as exc:
            raise UcrGatewayUnavailable(
                "aiohttp is required for the UCR HTTP transport"
            ) from exc

        timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
        kwargs: dict[str, Any] = {"headers": dict(headers)}
        if payload is not None:
            kwargs["json"] = dict(payload)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, **kwargs) as response:
                    declared = response.headers.get("Content-Length")
                    if declared:
                        try:
                            declared_bytes = int(declared)
                        except ValueError as exc:
                            raise UcrGatewayProtocolError(
                                "UCR gateway returned an invalid content length"
                            ) from exc
                        if declared_bytes < 0 or declared_bytes > max_response_bytes:
                            raise UcrGatewayProtocolError(
                                "UCR gateway response exceeded the configured limit"
                            )
                    body = await response.content.read(max_response_bytes + 1)
                    if len(body) > max_response_bytes:
                        raise UcrGatewayProtocolError(
                            "UCR gateway response exceeded the configured limit"
                        )
                    return response.status, dict(response.headers), body
        except UcrGatewayError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise UcrGatewayUnavailable("UCR gateway request failed") from exc


class UcrGatewayClient:
    """Fail-closed adapter to a thin gateway over UCR public services.

    ClientPlatform owns tenant, CRM, automation, consent, billing and provider-delivery
    semantics. This adapter may invoke only versioned public UCR RPCs. It deliberately
    does not infer Device, Group, membership or CallSession state that the public UCR
    service contract did not receive from the caller.
    """

    def __init__(
        self,
        *,
        config: UcrGatewayConfig | None = None,
        credential_provider: CredentialProvider | None = None,
        transport: UcrGatewayTransport | None = None,
    ) -> None:
        self.config = config or ucr_gateway_config()
        self._credential_provider = credential_provider or EnvironmentCredentialProvider()
        self._transport = transport or AiohttpUcrGatewayTransport()

    async def health(self) -> dict[str, Any]:
        return await self._request(method="GET", path="/v1/health")

    async def invoke_integration(
        self,
        *,
        method: UcrIntegrationMethod,
        request: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        normalized_method = _require_enum(method, UcrIntegrationMethod)
        return await self._invoke_rpc(
            service=UCR_INTEGRATION_SERVICE,
            method=normalized_method.value,
            request=request,
            mutating=normalized_method in _INTEGRATION_MUTATIONS,
            idempotency_key=idempotency_key,
        )

    async def invoke_call(
        self,
        *,
        method: UcrCallMethod,
        request: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        normalized_method = _require_enum(method, UcrCallMethod)
        return await self._invoke_rpc(
            service=UCR_CALL_SERVICE,
            method=normalized_method.value,
            request=request,
            mutating=normalized_method in _CALL_MUTATIONS,
            idempotency_key=idempotency_key,
        )

    async def invoke_universal_conference(
        self,
        *,
        method: UcrUniversalConferenceMethod,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized_method = _require_enum(method, UcrUniversalConferenceMethod)
        return await self._invoke_rpc(
            service=UCR_UNIVERSAL_CONFERENCE_SERVICE,
            method=normalized_method.value,
            request=request,
            mutating=False,
            idempotency_key=None,
        )

    async def _invoke_rpc(
        self,
        *,
        service: str,
        method: str,
        request: Mapping[str, Any],
        mutating: bool,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        request_object = _normalize_rpc_request(request)
        if mutating:
            normalized_key = _normalize_idempotency_key(idempotency_key)
        elif idempotency_key is not None:
            raise ValueError("read-only UCR RPC must not use an idempotency key")
        else:
            normalized_key = None
        payload = {
            "version": 1,
            "service": service,
            "method": method,
            "request": request_object,
        }
        return await self._request(
            method="POST",
            path=_RPC_PATH,
            payload=payload,
            idempotency_key=normalized_key,
            expected_service=service,
            expected_method=method,
        )

    async def _request(
        self,
        *,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_service: str | None = None,
        expected_method: str | None = None,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            raise UcrGatewayConfigurationError("UCR gateway is disabled")
        token = self._resolve_token()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "X-ClientPlatform-UCR-Revision": self.config.pinned_revision,
        }
        if payload is not None:
            encoded = _encode_request(payload)
            if len(encoded) > _MAX_REQUEST_BYTES:
                raise ValueError("UCR gateway request payload exceeds 65536 bytes")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            status, _response_headers, body = await self._transport.request(
                method=method,
                url=f"{self.config.base_url}{path}",
                headers=headers,
                payload=payload,
                timeout_seconds=self.config.timeout_seconds,
                max_response_bytes=self.config.max_response_bytes,
            )
        except UcrGatewayError:
            raise
        except (asyncio.TimeoutError, OSError) as exc:
            raise UcrGatewayUnavailable("UCR gateway request failed") from exc
        response = _decode_response(body)
        if status in {401, 403, 409, 422}:
            raise UcrGatewayRejected(_safe_error_code(response))
        if status == 429 or 500 <= status <= 599:
            raise UcrGatewayUnavailable(_safe_error_code(response))
        if status < 200 or status >= 300:
            raise UcrGatewayProtocolError(_safe_error_code(response))
        if response.get("ok") is not True:
            raise UcrGatewayProtocolError("ucr_gateway_response_not_ok")
        revision = str(response.get("ucr_revision") or "").strip().lower()
        if revision != self.config.pinned_revision:
            raise UcrGatewayProtocolError("ucr_gateway_revision_mismatch")
        if expected_service is not None:
            _validate_rpc_response(
                response,
                expected_service=expected_service,
                expected_method=expected_method,
            )
        return response

    def _resolve_token(self) -> str:
        try:
            token = str(
                self._credential_provider.resolve(self.config.token_reference) or ""
            ).strip()
        except SecretReferenceError as exc:
            raise UcrGatewayUnavailable(
                "UCR gateway credential is unavailable"
            ) from exc
        if len(token.encode("utf-8")) < 32:
            raise UcrGatewayUnavailable("UCR gateway credential is unavailable")
        return token


def ucr_gateway_config(environment: Mapping[str, str] | None = None) -> UcrGatewayConfig:
    env = environment if environment is not None else os.environ
    enabled = (
        str(env.get("CLIENTPLATFORM_UCR_GATEWAY_ENABLED") or "").strip().lower()
        in _TRUE_VALUES
    )
    base_url = str(env.get("CLIENTPLATFORM_UCR_GATEWAY_URL") or "").strip()
    token_reference = str(
        env.get("CLIENTPLATFORM_UCR_GATEWAY_TOKEN_REFERENCE")
        or _DEFAULT_TOKEN_REFERENCE
    ).strip()
    raw_timeout = str(
        env.get("CLIENTPLATFORM_UCR_GATEWAY_TIMEOUT_SEC")
        or _DEFAULT_TIMEOUT_SECONDS
    ).strip()
    raw_limit = str(
        env.get("CLIENTPLATFORM_UCR_GATEWAY_MAX_RESPONSE_BYTES")
        or _DEFAULT_MAX_RESPONSE_BYTES
    ).strip()
    try:
        timeout_seconds = float(raw_timeout)
        max_response_bytes = int(raw_limit)
    except ValueError as exc:
        raise UcrGatewayConfigurationError(
            "UCR gateway numeric configuration is invalid"
        ) from exc
    return UcrGatewayConfig(
        enabled=enabled,
        base_url=base_url,
        token_reference=token_reference,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
    )


def _normalize_base_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UcrGatewayConfigurationError("UCR gateway URL must be absolute HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise UcrGatewayConfigurationError("UCR gateway URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise UcrGatewayConfigurationError(
            "UCR gateway URL must not contain query or fragment"
        )
    host = str(parsed.hostname).lower()
    if parsed.scheme != "https" and host not in _LOOPBACK_HOSTS:
        raise UcrGatewayConfigurationError("UCR gateway requires HTTPS outside loopback")
    normalized_path = parsed.path.rstrip("/")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, normalized_path, "", "")
    ).rstrip("/")


def _is_secret_reference(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw or len(raw) > 512 or any(character.isspace() for character in raw):
        return False
    return raw.startswith(("secret://env/", "vault://connection/"))


def _require_enum(value: _EnumT, enum_type: type[_EnumT]) -> _EnumT:
    if not isinstance(value, enum_type):
        raise ValueError(f"unsupported UCR method for {enum_type.__name__}")
    return value


def _normalize_rpc_request(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ValueError("UCR RPC request must be a mapping")
    normalized = dict(request)
    if not normalized:
        raise ValueError("UCR RPC request must not be empty")
    if not all(isinstance(key, str) and key for key in normalized):
        raise ValueError("UCR RPC request keys must be non-empty strings")
    _encode_request(normalized)
    return normalized


def _normalize_idempotency_key(value: str | None) -> str:
    raw = str(value or "").strip()
    if not _IDEMPOTENCY_KEY_RE.fullmatch(raw):
        raise ValueError("UCR gateway idempotency key is invalid")
    return raw


def _encode_request(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("UCR gateway request must be valid finite JSON") from exc


def _decode_response(body: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UcrGatewayProtocolError("UCR gateway returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise UcrGatewayProtocolError("UCR gateway response must be a JSON object")
    return decoded


def _validate_rpc_response(
    response: Mapping[str, Any],
    *,
    expected_service: str,
    expected_method: str | None,
) -> None:
    if response.get("service") != expected_service:
        raise UcrGatewayProtocolError("ucr_gateway_service_mismatch")
    if expected_method is None or response.get("method") != expected_method:
        raise UcrGatewayProtocolError("ucr_gateway_method_mismatch")
    if not isinstance(response.get("result"), Mapping):
        raise UcrGatewayProtocolError("ucr_gateway_result_missing")


def _safe_error_code(response: Mapping[str, Any]) -> str:
    raw = str(response.get("error") or "ucr_gateway_rejected").strip().lower()
    if not re.fullmatch(r"[a-z0-9_.:-]{1,96}", raw):
        return "ucr_gateway_rejected"
    return raw


__all__ = [
    "AiohttpUcrGatewayTransport",
    "UCR_CALL_SERVICE",
    "UCR_INTEGRATION_SERVICE",
    "UCR_PINNED_REVISION",
    "UCR_UNIVERSAL_CONFERENCE_SERVICE",
    "UcrCallMethod",
    "UcrGatewayClient",
    "UcrGatewayConfig",
    "UcrGatewayConfigurationError",
    "UcrGatewayError",
    "UcrGatewayProtocolError",
    "UcrGatewayRejected",
    "UcrGatewayUnavailable",
    "UcrIntegrationMethod",
    "UcrUniversalConferenceMethod",
    "ucr_gateway_config",
]