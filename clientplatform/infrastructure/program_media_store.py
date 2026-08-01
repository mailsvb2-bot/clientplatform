from __future__ import annotations

"""Verified bounded-memory storage for program lesson media.

The running application stores only private ``s3://`` references.  Access keys,
provider URLs and temporary Telegram download paths never enter the program
record or an exception message.
"""

import hashlib
import hmac
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from clientplatform.domain.programs import ContentKind
from clientplatform.domain.tenancy import normalize_uuid

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_EXTENSION_RE = re.compile(r"^[a-z0-9]{1,10}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_DEFAULT_MAX_BYTES = 20_000_000
_DEFAULT_CHUNK_BYTES = 1024 * 1024
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class ProgramMediaStoreError(RuntimeError):
    """Sanitized media-storage failure safe for user-facing handlers and logs."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        cleanup_reference: str = "",
    ) -> None:
        normalized = str(code or "program_media_store_failure").strip()[:120]
        super().__init__(normalized)
        self.code = normalized
        self.retryable = bool(retryable)
        self.cleanup_reference = str(cleanup_reference or "").strip()


@dataclass(frozen=True, slots=True)
class ProgramMediaStoreConfig:
    enabled: bool
    endpoint_host: str
    endpoint_path: str
    region: str
    bucket: str
    access_key: str
    secret_key: str
    session_token: str
    timeout_seconds: float
    max_bytes: int


@dataclass(frozen=True, slots=True)
class StoredProgramMedia:
    reference: str
    object_key: str
    size: int
    sha256: str


class FileChunkBody:
    """Re-openable bounded iterator accepted by urllib/http.client."""

    def __init__(self, path: Path, *, chunk_bytes: int = _DEFAULT_CHUNK_BYTES) -> None:
        self.path = path
        self.chunk_bytes = int(chunk_bytes)
        if self.chunk_bytes < 64 * 1024 or self.chunk_bytes > 8 * 1024 * 1024:
            raise ValueError("chunk_bytes must be between 64 KiB and 8 MiB")

    def __iter__(self) -> Iterable[bytes]:
        with self.path.open("rb") as source:
            while True:
                chunk = source.read(self.chunk_bytes)
                if not chunk:
                    return
                yield chunk


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def _required(values: Mapping[str, str], name: str) -> str:
    value = str(values.get(name, "") or "").strip()
    if not value or value.lower() in {"changeme", "change-me", "secret", "password"}:
        raise ProgramMediaStoreError(f"program_media_missing_{name.lower()}")
    return value


def program_media_store_config(
    env: Mapping[str, str] | None = None,
) -> ProgramMediaStoreConfig:
    values = os.environ if env is None else env
    enabled = _truthy(values.get("CLIENTPLATFORM_PROGRAM_MEDIA_INGEST_ENABLED"))
    if not enabled:
        return ProgramMediaStoreConfig(
            enabled=False,
            endpoint_host="",
            endpoint_path="",
            region="",
            bucket="",
            access_key="",
            secret_key="",
            session_token="",
            timeout_seconds=30.0,
            max_bytes=_DEFAULT_MAX_BYTES,
        )

    endpoint = _required(values, "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT").rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ProgramMediaStoreError("program_media_endpoint_requires_https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProgramMediaStoreError("program_media_endpoint_invalid")

    bucket = _required(values, "CLIENTPLATFORM_STORAGE_BUCKET").lower()
    if not _BUCKET_RE.fullmatch(bucket) or not bucket.startswith("clientplatform-"):
        raise ProgramMediaStoreError("program_media_bucket_invalid")

    region = _required(values, "CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION")
    if len(region) > 64:
        raise ProgramMediaStoreError("program_media_region_invalid")

    try:
        timeout_seconds = float(
            str(values.get("CLIENTPLATFORM_PROGRAM_MEDIA_TIMEOUT_SEC", "30") or "30")
        )
        max_bytes = int(
            str(
                values.get(
                    "CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES",
                    str(_DEFAULT_MAX_BYTES),
                )
                or str(_DEFAULT_MAX_BYTES)
            )
        )
    except (TypeError, ValueError):
        raise ProgramMediaStoreError("program_media_numeric_configuration_invalid") from None
    if timeout_seconds <= 0 or timeout_seconds > 120:
        raise ProgramMediaStoreError("program_media_timeout_invalid")
    if max_bytes <= 0 or max_bytes > _DEFAULT_MAX_BYTES:
        raise ProgramMediaStoreError("program_media_size_limit_invalid")

    return ProgramMediaStoreConfig(
        enabled=True,
        endpoint_host=parsed.netloc,
        endpoint_path=parsed.path.rstrip("/"),
        region=region,
        bucket=bucket,
        access_key=_required(values, "CLIENTPLATFORM_SECRET_S3_ACCESS_KEY"),
        secret_key=_required(values, "CLIENTPLATFORM_SECRET_S3_SECRET_KEY"),
        session_token=str(
            values.get("CLIENTPLATFORM_SECRET_S3_SESSION_TOKEN", "") or ""
        ).strip(),
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
    )


def _normalize_header_value(value: str) -> str:
    return " ".join(str(value).strip().split())


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    date_key = hmac.new(
        f"AWS4{secret_key}".encode("utf-8"),
        date_stamp.encode("ascii"),
        hashlib.sha256,
    ).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _authorization_headers(
    *,
    method: str,
    host: str,
    path: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
    payload_sha256: str,
    extra_headers: Mapping[str, str],
    now: datetime,
) -> dict[str, str]:
    if not _SHA256_RE.fullmatch(payload_sha256):
        raise ValueError("invalid payload SHA-256")
    amz_date = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    for name, value in extra_headers.items():
        lowered = str(name).strip().lower()
        if lowered in {"authorization", "host", "x-amz-date", "x-amz-content-sha256"}:
            continue
        headers[lowered] = _normalize_header_value(value)

    signed_names = sorted(headers)
    canonical_headers = "".join(
        f"{name}:{_normalize_header_value(headers[name])}\n" for name in signed_names
    )
    signed_headers = ";".join(signed_names)
    canonical_request = "\n".join(
        (
            method.upper(),
            quote(path or "/", safe="/-_.~"),
            "",
            canonical_headers,
            signed_headers,
            payload_sha256,
        )
    )
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        )
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    result = {name: value for name, value in headers.items() if name != "host"}
    result["Authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_DEFAULT_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extension(value: str) -> str:
    normalized = str(value or "bin").strip().lower().lstrip(".")
    return normalized if _EXTENSION_RE.fullmatch(normalized) else "bin"


def _headers_lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in headers.items()}


class ProgramMediaStore:
    def __init__(
        self,
        config: ProgramMediaStoreConfig,
        *,
        opener: Callable[..., Any] = urlopen,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        chunk_bytes: int = _DEFAULT_CHUNK_BYTES,
    ) -> None:
        self.config = config
        self._opener = opener
        self._clock = clock
        self._chunk_bytes = int(chunk_bytes)

    def _request_url(self, key: str) -> tuple[str, str]:
        path = f"{self.config.endpoint_path}/{self.config.bucket}/{key}" or "/"
        url = urlunsplit(
            (
                "https",
                self.config.endpoint_host,
                quote(path, safe="/-_.~%"),
                "",
                "",
            )
        )
        return path, url

    def put_file(
        self,
        path: Path,
        *,
        business_id: str,
        content_kind: ContentKind,
        content_type: str,
        extension: str,
    ) -> StoredProgramMedia:
        if not self.config.enabled:
            raise ProgramMediaStoreError("program_media_ingest_disabled")
        normalized_business_id = normalize_uuid(business_id, field_name="business_id")
        selected = path.expanduser().resolve(strict=True)
        if not selected.is_file() or selected.is_symlink():
            raise ProgramMediaStoreError("program_media_source_invalid")
        size = selected.stat().st_size
        if size <= 0:
            raise ProgramMediaStoreError("program_media_source_empty")
        if size > self.config.max_bytes:
            raise ProgramMediaStoreError("program_media_source_too_large")

        sha256 = _sha256_file(selected)
        business_scope = hashlib.sha256(
            normalized_business_id.encode("ascii")
        ).hexdigest()[:20]
        object_key = (
            f"program-media/{business_scope}/{content_kind.value}/"
            f"{sha256[:2]}/{sha256}-{uuid4().hex[:12]}.{_safe_extension(extension)}"
        )
        cleanup_reference = f"s3://{self.config.bucket}/{object_key}"
        normalized_type = str(content_type or "application/octet-stream").strip()
        if not normalized_type or "\r" in normalized_type or "\n" in normalized_type:
            normalized_type = "application/octet-stream"
        headers = {
            "content-length": str(size),
            "content-type": normalized_type[:120],
            "x-amz-meta-clientplatform-sha256": sha256,
            "x-amz-meta-clientplatform-size": str(size),
            "x-amz-meta-clientplatform-kind": content_kind.value,
        }
        request_path, url = self._request_url(object_key)
        signed = _authorization_headers(
            method="PUT",
            host=self.config.endpoint_host,
            path=request_path,
            region=self.config.region,
            access_key=self.config.access_key,
            secret_key=self.config.secret_key,
            session_token=self.config.session_token,
            payload_sha256=sha256,
            extra_headers=headers,
            now=self._clock(),
        )
        signed.update(headers)
        request = Request(
            url,
            data=FileChunkBody(selected, chunk_bytes=self._chunk_bytes),
            headers=signed,
            method="PUT",
        )
        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as response:
                status = int(response.status)
                if status not in {200, 201}:
                    raise ProgramMediaStoreError(
                        "program_media_upload_status_invalid",
                        retryable=status >= 500,
                        cleanup_reference=cleanup_reference,
                    )
                response.read(65_536)
        except ProgramMediaStoreError:
            raise
        except HTTPError as exc:
            exc.read(65_536)
            raise ProgramMediaStoreError(
                "program_media_upload_http_failure",
                retryable=exc.code >= 500 or exc.code == 429,
                cleanup_reference=cleanup_reference,
            ) from None
        except (URLError, TimeoutError, OSError):
            raise ProgramMediaStoreError(
                "program_media_upload_transport_failure",
                retryable=True,
                cleanup_reference=cleanup_reference,
            ) from None

        head_headers = {
            "content-length": "0",
        }
        head_signed = _authorization_headers(
            method="HEAD",
            host=self.config.endpoint_host,
            path=request_path,
            region=self.config.region,
            access_key=self.config.access_key,
            secret_key=self.config.secret_key,
            session_token=self.config.session_token,
            payload_sha256=_EMPTY_SHA256,
            extra_headers=head_headers,
            now=self._clock(),
        )
        head_signed.update(head_headers)
        try:
            with self._opener(
                Request(url, headers=head_signed, method="HEAD"),
                timeout=self.config.timeout_seconds,
            ) as response:
                status = int(response.status)
                returned = _headers_lower(response.headers)
                if status != 200:
                    raise ProgramMediaStoreError(
                        "program_media_verify_status_invalid",
                        retryable=status >= 500,
                        cleanup_reference=cleanup_reference,
                    )
        except ProgramMediaStoreError:
            raise
        except HTTPError as exc:
            exc.read(65_536)
            raise ProgramMediaStoreError(
                "program_media_verify_http_failure",
                retryable=exc.code >= 500 or exc.code == 429,
                cleanup_reference=cleanup_reference,
            ) from None
        except (URLError, TimeoutError, OSError):
            raise ProgramMediaStoreError(
                "program_media_verify_transport_failure",
                retryable=True,
                cleanup_reference=cleanup_reference,
            ) from None

        if (
            returned.get("content-length") != str(size)
            or returned.get("x-amz-meta-clientplatform-sha256") != sha256
            or returned.get("x-amz-meta-clientplatform-size") != str(size)
            or returned.get("x-amz-meta-clientplatform-kind") != content_kind.value
        ):
            raise ProgramMediaStoreError(
                "program_media_upload_verification_failed",
                cleanup_reference=cleanup_reference,
            )
        return StoredProgramMedia(
            reference=cleanup_reference,
            object_key=object_key,
            size=size,
            sha256=sha256,
        )


__all__ = [
    "FileChunkBody",
    "ProgramMediaStore",
    "ProgramMediaStoreConfig",
    "ProgramMediaStoreError",
    "StoredProgramMedia",
    "program_media_store_config",
]
