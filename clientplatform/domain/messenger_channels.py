from __future__ import annotations

import re
from dataclasses import dataclass

from clientplatform.domain.connections import (
    ConnectionPlatform,
    normalize_connection_platform,
    normalize_credential_reference,
    normalize_external_account_id,
)
from clientplatform.domain.customers import CustomerPlatform
from clientplatform.domain.tenancy import normalize_uuid


class MessengerChannelError(RuntimeError):
    """Base error for canonical messenger ingress/linking."""


class MessengerRouteNotFound(MessengerChannelError):
    """No active, uniquely scoped provider route exists."""


class CustomerChannelLinkRejected(MessengerChannelError):
    """A cross-channel customer identity link was rejected fail-closed."""


class CustomerChannelIdentityConflict(CustomerChannelLinkRejected):
    """The external identity is already owned by another canonical customer."""


_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{24,160}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_LINK_COMMAND_RE = re.compile(
    r"^(?:(?:/start|start)\s+)?cplink_(?P<token>[A-Za-z0-9_-]{24,160})$",
    re.IGNORECASE,
)


def normalize_customer_link_token(value: str) -> str:
    token = str(value or "").strip()
    if not _TOKEN_RE.fullmatch(token):
        raise CustomerChannelLinkRejected("customer link token is invalid")
    return token


def extract_customer_link_token(text: str | None) -> str | None:
    normalized = " ".join(str(text or "").strip().split())
    match = _LINK_COMMAND_RE.fullmatch(normalized)
    if match is None:
        return None
    return normalize_customer_link_token(match.group("token"))


def normalize_token_digest(value: str) -> str:
    digest = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(digest):
        raise ValueError("token digest must be lowercase SHA-256")
    return digest


def normalize_customer_platform(value: CustomerPlatform | ConnectionPlatform | str) -> CustomerPlatform:
    raw = value.value if isinstance(value, (CustomerPlatform, ConnectionPlatform)) else str(value)
    try:
        return CustomerPlatform(str(raw).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unsupported customer platform: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class CustomerIngressContext:
    """Server-resolved provider route for one business/customer ingress."""

    business_id: str
    connection_id: str
    platform: CustomerPlatform

    def __post_init__(self) -> None:
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(
            self,
            "connection_id",
            normalize_uuid(self.connection_id, field_name="connection_id"),
        )
        object.__setattr__(self, "platform", normalize_customer_platform(self.platform))


@dataclass(frozen=True, slots=True)
class MessengerIngressRoute:
    id: str
    business_id: str
    connection_id: str
    platform: ConnectionPlatform
    external_route_id: str
    webhook_secret_reference: str
    status: str
    created_by_member_id: str
    created_at: str
    updated_at: str
    confirmation_code_reference: str | None = None
    revoked_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="messenger_route_id"))
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(
            self,
            "connection_id",
            normalize_uuid(self.connection_id, field_name="connection_id"),
        )
        object.__setattr__(
            self,
            "created_by_member_id",
            normalize_uuid(self.created_by_member_id, field_name="created_by_member_id"),
        )
        platform = normalize_connection_platform(self.platform)
        if platform not in {ConnectionPlatform.VK, ConnectionPlatform.MAX}:
            raise ValueError("messenger ingress route supports only VK or MAX")
        object.__setattr__(self, "platform", platform)
        object.__setattr__(
            self,
            "external_route_id",
            normalize_external_account_id(self.external_route_id),
        )
        object.__setattr__(
            self,
            "webhook_secret_reference",
            normalize_credential_reference(self.webhook_secret_reference),
        )
        confirmation_reference = self.confirmation_code_reference
        if platform == ConnectionPlatform.VK:
            if confirmation_reference is None:
                raise ValueError("VK messenger ingress route requires confirmation code reference")
            object.__setattr__(
                self,
                "confirmation_code_reference",
                normalize_credential_reference(confirmation_reference),
            )
        elif confirmation_reference is not None:
            raise ValueError("MAX messenger ingress route must not define VK confirmation code reference")
        status = str(self.status or "").strip().lower()
        if status not in {"active", "disabled", "revoked"}:
            raise ValueError("unsupported messenger ingress route status")
        object.__setattr__(self, "status", status)

    @property
    def customer_context(self) -> CustomerIngressContext:
        return CustomerIngressContext(
            business_id=self.business_id,
            connection_id=self.connection_id,
            platform=self.platform.value,
        )


@dataclass(frozen=True, slots=True)
class IssuedCustomerLink:
    """One-time token returned only at issuance; only its digest is stored."""

    token: str
    business_id: str
    customer_id: str
    target_platform: CustomerPlatform | None
    expires_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "token", normalize_customer_link_token(self.token))
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(self, "customer_id", normalize_uuid(self.customer_id, field_name="customer_id"))
        if self.target_platform is not None:
            object.__setattr__(self, "target_platform", normalize_customer_platform(self.target_platform))
