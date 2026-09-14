from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping

UCR_PINNED_REVISION = "8097b41e69634c944c225f7071e80b991d4ddc02"
UCR_INTEGRATION_SERVICE = "ucr.v1.IntegrationService"
UCR_CALL_SERVICE = "ucr.v1.CallService"
SERVICE_CREDENTIAL_ID_METADATA_KEY = "ucr-service-credential-id-bin"
SERVICE_CREDENTIAL_SECRET_METADATA_KEY = "ucr-service-credential-secret-bin"

MAX_HTTP_BODY_BYTES = 64 * 1024
MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")

INTEGRATION_REQUEST_TYPES = {
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
CALL_REQUEST_TYPES = {
    "StartCall": "CallStartRequest",
    "GetCall": "CallGetRequest",
    "SignalCall": "CallSignalRequest",
}
MUTATING_METHODS = frozenset(
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


class GatewayUpstreamError(RuntimeError):
    def __init__(self, status_code: int, error_code: str) -> None:
        super().__init__(error_code)
        self.status_code = int(status_code)
        self.error_code = str(error_code)


def decode_json_object(body: bytes) -> dict[str, Any]:
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
    assert_finite_json(decoded)
    return decoded


def validate_envelope(
    envelope: Mapping[str, Any], headers: Mapping[str, str]
) -> tuple[str, str, dict[str, Any], str | None]:
    if envelope.get("version") != 1:
        raise ValueError("ucr_gateway_version_invalid")
    service = str(envelope.get("service") or "").strip()
    method = str(envelope.get("method") or "").strip()
    request = envelope.get("request")
    if service == UCR_INTEGRATION_SERVICE:
        allowed = INTEGRATION_REQUEST_TYPES
    elif service == UCR_CALL_SERVICE:
        allowed = CALL_REQUEST_TYPES
    else:
        raise ValueError("ucr_rpc_service_not_allowed")
    if method not in allowed:
        raise ValueError("ucr_rpc_method_not_allowed")
    if not isinstance(request, dict) or not request:
        raise ValueError("ucr_rpc_request_invalid")
    assert_finite_json(request)

    raw_key = str(headers.get("Idempotency-Key") or "").strip()
    if (service, method) in MUTATING_METHODS:
        if not IDEMPOTENCY_KEY_RE.fullmatch(raw_key):
            raise ValueError("ucr_gateway_idempotency_key_invalid")
        idempotency_key: str | None = raw_key
    else:
        if raw_key:
            raise ValueError("ucr_gateway_read_idempotency_forbidden")
        idempotency_key = None
    return service, method, dict(request), idempotency_key


def assert_finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("ucr_gateway_request_nonfinite")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("ucr_gateway_request_key_invalid")
            assert_finite_json(child)
    elif isinstance(value, list):
        for child in value:
            assert_finite_json(child)


def encode_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_error_to_gateway_error(error: Mapping[str, Any]) -> GatewayUpstreamError:
    code = str(error.get("code") or "ERROR_CODE_UNSPECIFIED").strip()
    status_code, error_code = _UCR_CANONICAL_ERROR_MAP.get(
        code, (502, "ucr_error_unspecified")
    )
    return GatewayUpstreamError(status_code, error_code)
