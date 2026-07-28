from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from clientplatform.domain.programs import ContentKind
from clientplatform.transport.base import CredentialProvider


class MediaReferenceError(RuntimeError):
    """A media reference cannot be safely exposed to a transport provider."""

    retryable = False


class MediaReferenceResolver(Protocol):
    async def resolve(self, reference: str, kind: ContentKind) -> str:
        """Return one provider-safe, short-lived media reference."""


_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_SIGNATURE_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def _provider_safe_reference(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise MediaReferenceError("media_reference_empty")
    if any(ord(char) < 32 for char in normalized):
        raise MediaReferenceError("media_reference_control_character")
    if "://" not in normalized:
        return normalized
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.netloc:
        raise MediaReferenceError("media_reference_requires_https")
    if parsed.username or parsed.password or parsed.fragment:
        raise MediaReferenceError("media_reference_url_invalid")
    return normalized


def parse_s3_reference(reference: str) -> tuple[str, str]:
    parsed = urlsplit(str(reference or "").strip())
    if parsed.scheme != "s3" or not parsed.netloc:
        raise MediaReferenceError("media_storage_reference_invalid")
    bucket = parsed.netloc.lower()
    if not _BUCKET_RE.fullmatch(bucket):
        raise MediaReferenceError("media_storage_bucket_invalid")
    key = parsed.path.lstrip("/")
    if not key or len(key) > 1024:
        raise MediaReferenceError("media_storage_key_invalid")
    segments = key.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise MediaReferenceError("media_storage_key_invalid")
    if parsed.query or parsed.fragment or any(ord(char) < 32 for char in key):
        raise MediaReferenceError("media_storage_key_invalid")
    return bucket, key


def media_gateway_signature(*, secret: str, path: str, expires: int) -> str:
    normalized_secret = str(secret or "").strip()
    normalized_path = str(path or "").strip()
    if not normalized_secret:
        raise MediaReferenceError("media_signing_secret_unavailable")
    if not normalized_path.startswith("/") or "?" in normalized_path or "#" in normalized_path:
        raise MediaReferenceError("media_gateway_path_invalid")
    canonical = f"GET\n{normalized_path}\n{int(expires)}".encode("utf-8")
    return base64.urlsafe_b64encode(
        hmac.new(normalized_secret.encode("utf-8"), canonical, hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")


def verify_media_gateway_signature(
    *,
    secret: str,
    path: str,
    expires: str | int,
    signature: str,
    clock: Callable[[], float] = time.time,
    max_future_seconds: int = 900,
) -> None:
    normalized_signature = str(signature or "").strip()
    if not _SIGNATURE_RE.fullmatch(normalized_signature):
        raise MediaReferenceError("media_gateway_signature_invalid")
    try:
        expiry = int(str(expires).strip())
    except (TypeError, ValueError):
        raise MediaReferenceError("media_gateway_expiry_invalid") from None
    now = int(clock())
    if expiry <= now:
        raise MediaReferenceError("media_gateway_url_expired")
    future_limit = max(60, min(int(max_future_seconds), 900))
    if expiry > now + future_limit:
        raise MediaReferenceError("media_gateway_expiry_invalid")
    expected = media_gateway_signature(secret=secret, path=path, expires=expiry)
    if not hmac.compare_digest(normalized_signature, expected):
        raise MediaReferenceError("media_gateway_signature_invalid")


class SafeMediaReferenceResolver:
    """Allow Telegram file IDs and HTTPS URLs, reject private storage schemes."""

    async def resolve(self, reference: str, kind: ContentKind) -> str:
        del kind
        return _provider_safe_reference(reference)


class HmacMediaGatewayResolver:
    """Convert private ``s3://`` references into short-lived gateway URLs.

    The signing secret is resolved for each send and never persisted. The
    gateway validates the same canonical string and streams the private object
    only until the expiry timestamp.
    """

    def __init__(
        self,
        *,
        base_url: str,
        credential_provider: CredentialProvider,
        signing_secret_reference: str,
        ttl_seconds: int = 300,
        clock: Callable[[], float] = time.time,
    ) -> None:
        parsed = urlsplit(str(base_url or "").strip().rstrip("/"))
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("media gateway base URL must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("media gateway base URL must not contain credentials or query")
        ttl = int(ttl_seconds)
        if ttl < 60 or ttl > 900:
            raise ValueError("media gateway TTL must be between 60 and 900 seconds")
        self._base_path = parsed.path.rstrip("/")
        self._base_url = urlunsplit(
            (parsed.scheme, parsed.netloc, self._base_path, "", "")
        )
        self._credential_provider = credential_provider
        self._signing_secret_reference = str(signing_secret_reference or "").strip()
        if not self._signing_secret_reference:
            raise ValueError("media signing secret reference is required")
        self._ttl_seconds = ttl
        self._clock = clock

    async def resolve(self, reference: str, kind: ContentKind) -> str:
        del kind
        normalized = str(reference or "").strip()
        if not normalized.startswith("s3://"):
            return _provider_safe_reference(normalized)

        bucket, key = parse_s3_reference(normalized)
        secret = str(
            await asyncio.to_thread(
                self._credential_provider.resolve,
                self._signing_secret_reference,
            )
            or ""
        ).strip()
        if not secret:
            raise MediaReferenceError("media_signing_secret_unavailable")

        expires = int(self._clock()) + self._ttl_seconds
        encoded_bucket = quote(bucket, safe="")
        encoded_key = quote(key, safe="/-_.~")
        relative_path = f"/media/{encoded_bucket}/{encoded_key}"
        signed_path = f"{self._base_path}{relative_path}"
        signature = media_gateway_signature(
            secret=secret,
            path=signed_path,
            expires=expires,
        )
        return f"{self._base_url}{relative_path}?expires={expires}&sig={signature}"
