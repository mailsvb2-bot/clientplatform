from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import UUID, uuid4, uuid5

from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.native_messenger_setup_repository import (
    NativeMessengerSetupRejected,
    NativeMessengerSetupRepository,
)
from clientplatform.transport.base import CredentialProvider
from services.db import get_db, get_db_ro


_COMMAND_PREFIX = "cpm:setup:"
_DOMAIN = b"clientplatform/native-messenger-setup/v1\x00"
_DEFAULT_SIGNING_REFERENCE = "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
_IDEMPOTENCY_NAMESPACE = UUID("ed2c55fb-eacd-41d2-82b5-31f332379c19")


class NativeMessengerSetupLinkRejected(RuntimeError):
    """A recoverable setup-link command is invalid or no longer authorized."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _session_id(value: str) -> str:
    try:
        return str(UUID(str(value or "").strip()))
    except (ValueError, AttributeError) as exc:
        raise NativeMessengerSetupLinkRejected("setup session reference is invalid") from exc


def _platform(value: ConnectionPlatform | str) -> ConnectionPlatform:
    try:
        platform = (
            value
            if isinstance(value, ConnectionPlatform)
            else ConnectionPlatform(str(value or "").strip().lower())
        )
    except ValueError as exc:
        raise NativeMessengerSetupLinkRejected("unsupported setup platform") from exc
    if platform not in {ConnectionPlatform.VK, ConnectionPlatform.MAX}:
        raise NativeMessengerSetupLinkRejected("setup supports only VK or MAX")
    return platform


def _idempotency_key(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 500:
        raise NativeMessengerSetupLinkRejected(
            "setup idempotency key must be 1..500 characters"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise NativeMessengerSetupLinkRejected(
            "setup idempotency key contains control characters"
        )
    return raw


def _secret_bytes(value: str) -> bytes:
    secret = str(value or "").strip().encode("utf-8")
    if len(secret) < 32:
        raise NativeMessengerSetupLinkRejected(
            "setup signing secret must contain at least 32 bytes"
        )
    return secret


def derive_native_setup_token(
    *,
    signing_secret: str,
    session_id: str,
    expires_at: str,
) -> str:
    """Derive a URL-safe bearer token under a setup-specific HMAC domain."""

    normalized_session = _session_id(session_id)
    normalized_expiry = str(expires_at or "").strip()
    if not normalized_expiry:
        raise NativeMessengerSetupLinkRejected("setup expiry is missing")
    material = (
        _DOMAIN
        + normalized_session.encode("ascii")
        + b"\x00"
        + normalized_expiry.encode("ascii")
    )
    digest = hmac.new(
        _secret_bytes(signing_secret),
        material,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_native_setup_command(session_id: str) -> str:
    return _COMMAND_PREFIX + _session_id(session_id)


def parse_native_setup_command(command: str) -> str | None:
    raw = str(command or "").strip()
    if not raw.startswith(_COMMAND_PREFIX):
        return None
    return _session_id(raw[len(_COMMAND_PREFIX) :])


class NativeMessengerSetupLinkService:
    """Issue non-durable setup references and materialize bearer URLs just in time."""

    def __init__(
        self,
        *,
        credential_provider: CredentialProvider,
        public_base_url: str | None = None,
        signing_secret_reference: str | None = None,
    ) -> None:
        self._credential_provider = credential_provider
        self._public_base_url = str(
            public_base_url
            if public_base_url is not None
            else (
                os.getenv("MESSENGER_PUBLIC_BASE_URL")
                or os.getenv("CLIENTPLATFORM_PUBLIC_BASE_URL")
                or ""
            )
        ).strip().rstrip("/")
        self._signing_secret_reference = str(
            signing_secret_reference
            if signing_secret_reference is not None
            else (
                os.getenv("CLIENTPLATFORM_MEDIA_SIGNING_SECRET_REFERENCE")
                or _DEFAULT_SIGNING_REFERENCE
            )
        ).strip()

    def _signing_secret(self) -> str:
        if not self._signing_secret_reference:
            raise NativeMessengerSetupLinkRejected(
                "setup signing secret reference is missing"
            )
        secret = self._credential_provider.resolve(self._signing_secret_reference)
        _secret_bytes(secret)
        return secret

    def _base_url(self) -> str:
        if not self._public_base_url.startswith("https://"):
            raise NativeMessengerSetupLinkRejected(
                "native messenger setup requires an HTTPS public base URL"
            )
        return self._public_base_url

    def issue_command(
        self,
        *,
        actor: TenantContext,
        platform: ConnectionPlatform | str,
        ttl_seconds: int = 600,
        idempotency_key: str | None = None,
    ) -> str:
        """Create a digest-only setup session and return a non-secret outbox command.

        When an idempotency key is supplied, the session UUID is stable for that
        exact tenant/member/platform work item. Webhook replay therefore cannot
        invalidate a setup command that was already materialized in the outbox.
        """

        self._base_url()
        secret = self._signing_secret()
        selected_platform = _platform(platform)
        now = _utc_now()
        lifetime = max(60, min(int(ttl_seconds), 1800))
        expires_at = _iso(now + timedelta(seconds=lifetime))
        if idempotency_key is None:
            session_id = str(uuid4())
        else:
            raw_key = _idempotency_key(idempotency_key)
            session_id = str(
                uuid5(
                    _IDEMPOTENCY_NAMESPACE,
                    "|".join(
                        (
                            actor.business_id,
                            str(actor.user_id),
                            selected_platform.value,
                            raw_key,
                        )
                    ),
                )
            )
        token = derive_native_setup_token(
            signing_secret=secret,
            session_id=session_id,
            expires_at=expires_at,
        )
        with get_db() as conn:
            reference = NativeMessengerSetupRepository(
                conn
            ).ensure_recoverable_reference(
                actor=actor,
                platform=selected_platform,
                ttl_seconds=lifetime,
                now=now,
                session_id=session_id,
                token=token,
            )
        if reference.session_id != session_id:
            raise NativeMessengerSetupLinkRejected(
                "setup session materialization invariant failed"
            )
        return build_native_setup_command(session_id)

    def resolve_command_url(
        self,
        *,
        command: str,
        business_id: str,
    ) -> str | None:
        """Resolve only setup commands; ordinary callback commands pass through unchanged."""

        session_id = parse_native_setup_command(command)
        if session_id is None:
            return None
        try:
            with get_db_ro() as conn:
                reference = NativeMessengerSetupRepository(conn).inspect_reference(
                    session_id=session_id,
                    business_id=str(business_id or "").strip(),
                )
            token = derive_native_setup_token(
                signing_secret=self._signing_secret(),
                session_id=reference.session_id,
                expires_at=reference.expires_at,
            )
            observed_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(observed_digest, reference.token_digest):
                raise NativeMessengerSetupLinkRejected(
                    "setup session digest does not match derived capability"
                )
        except NativeMessengerSetupRejected as exc:
            raise NativeMessengerSetupLinkRejected(str(exc)) from exc
        return (
            self._base_url()
            + "/clientplatform/connect/"
            + quote(token, safe="")
        )


__all__ = [
    "NativeMessengerSetupLinkRejected",
    "NativeMessengerSetupLinkService",
    "build_native_setup_command",
    "derive_native_setup_token",
    "parse_native_setup_command",
]
