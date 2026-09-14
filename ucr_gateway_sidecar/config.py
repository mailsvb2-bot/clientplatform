from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Mapping

_DEFAULT_GRPC_TIMEOUT_SECONDS = 5.0
_HOST_PORT_RE = re.compile(r"^\[([^\]]+)\]:(\d{1,5})$|^([^:]+):(\d{1,5})$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class GatewayConfigurationError(RuntimeError):
    pass


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
        host, port = split_host_port(self.grpc_target)
        if not 1 <= port <= 65535:
            raise GatewayConfigurationError("UCR gRPC target port is invalid")
        if not self.grpc_tls_enabled and not is_loopback_host(host):
            raise GatewayConfigurationError("UCR gRPC TLS is required outside loopback")
        if not 0.25 <= float(self.grpc_timeout_seconds) <= 30.0:
            raise GatewayConfigurationError("UCR gRPC timeout must be 0.25..30 seconds")
        if not 1 <= int(self.bind_port) <= 65535:
            raise GatewayConfigurationError("gateway bind port is invalid")
        if self.grpc_root_ca_file and not self.grpc_tls_enabled:
            raise GatewayConfigurationError("UCR root CA requires TLS")


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


def split_host_port(value: str) -> tuple[str, int]:
    match = _HOST_PORT_RE.fullmatch(str(value or "").strip())
    if not match:
        raise GatewayConfigurationError("UCR gRPC target must be host:port")
    host = match.group(1) or match.group(3) or ""
    port = int(match.group(2) or match.group(4) or "0")
    if not host:
        raise GatewayConfigurationError("UCR gRPC target host is invalid")
    return host, port


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
