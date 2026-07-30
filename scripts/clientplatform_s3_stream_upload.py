from __future__ import annotations

"""Bounded-memory SigV4 file upload for S3-compatible object storage."""

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlunsplit
from urllib.request import Request, urlopen

from scripts.clientplatform_s3_replication import (
    ReplicationConfig,
    ReplicationError,
    S3Client,
    _canonical_query,
    _normalize_header_value,
    _signing_key,
    _utc_now,
)

_METADATA_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_DEFAULT_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class UploadedFile:
    key: str
    size: int
    sha256: str


class FileChunkBody:
    """Re-openable bounded iterator accepted by urllib/http.client as request data."""

    def __init__(self, path: Path, *, chunk_bytes: int = _DEFAULT_CHUNK_BYTES) -> None:
        self.path = path
        self.chunk_bytes = int(chunk_bytes)
        if self.chunk_bytes < 64 * 1024 or self.chunk_bytes > 16 * 1024 * 1024:
            raise ValueError("chunk_bytes must be between 64 KiB and 16 MiB")

    def __iter__(self) -> Iterable[bytes]:
        with self.path.open("rb") as source:
            while True:
                chunk = source.read(self.chunk_bytes)
                if not chunk:
                    return
                yield chunk


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_DEFAULT_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _headers_lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in headers.items()}


def _metadata_headers(metadata: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in metadata.items():
        normalized_name = str(name or "").strip().lower()
        normalized_value = _normalize_header_value(str(value or ""))
        if not _METADATA_NAME_RE.fullmatch(normalized_name):
            raise ValueError("invalid S3 metadata name")
        if not normalized_value or len(normalized_value) > 1024:
            raise ValueError("invalid S3 metadata value")
        result[f"x-amz-meta-{normalized_name}"] = normalized_value
    return result


def _authorization_headers_for_hash(
    *,
    method: str,
    host: str,
    path: str,
    query: Mapping[str, str],
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
    payload_sha256: str,
    extra_headers: Mapping[str, str],
    now: datetime,
) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
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
            _canonical_query(query),
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


class StreamingS3Uploader:
    def __init__(
        self,
        config: ReplicationConfig,
        *,
        opener: Callable[..., Any] = urlopen,
        clock: Callable[[], datetime] = _utc_now,
        chunk_bytes: int = _DEFAULT_CHUNK_BYTES,
        control_client: S3Client | None = None,
    ) -> None:
        self.config = config
        self._opener = opener
        self._clock = clock
        self._chunk_bytes = int(chunk_bytes)
        self._control = control_client or S3Client(config, clock=clock)

    def bucket_versioning(self, bucket: str) -> str:
        return self._control.bucket_versioning(bucket)

    def put_file(
        self,
        bucket: str,
        key: str,
        path: Path,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> UploadedFile:
        selected = path.expanduser().resolve(strict=True)
        if not selected.is_file():
            raise ValueError("backup upload source must be a regular file")
        size = selected.stat().st_size
        if size <= 0:
            raise ValueError("backup upload source must not be empty")
        if size > self.config.max_copy_bytes:
            raise ValueError("backup upload exceeds the configured single-object limit")
        normalized_key = str(key or "").strip().lstrip("/")
        if not normalized_key or "\x00" in normalized_key:
            raise ValueError("invalid backup object key")

        sha256 = sha256_file(selected)
        headers = {
            "content-length": str(size),
            "content-type": str(content_type or "application/octet-stream").strip(),
            **_metadata_headers(metadata),
            "x-amz-meta-clientplatform-sha256": sha256,
            "x-amz-meta-clientplatform-size": str(size),
        }
        request_path = f"{self.config.endpoint_path}/{bucket}/{normalized_key}" or "/"
        signed = _authorization_headers_for_hash(
            method="PUT",
            host=self.config.endpoint_host,
            path=request_path,
            query={},
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
            urlunsplit(
                (
                    "https",
                    self.config.endpoint_host,
                    quote(request_path, safe="/-_.~%"),
                    "",
                    "",
                )
            ),
            data=FileChunkBody(selected, chunk_bytes=self._chunk_bytes),
            headers=signed,
            method="PUT",
        )
        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as response:
                status = int(response.status)
                if status != 200:
                    raise ReplicationError("backup_upload_unexpected_s3_status", status=status)
                response.read(65_536)
        except HTTPError as exc:
            exc.read(65_536)
            raise ReplicationError("backup_upload_s3_http_error", status=exc.code) from None
        except (URLError, TimeoutError, OSError):
            raise ReplicationError("backup_upload_s3_transport_failure") from None

        selected_headers = _headers_lower(self._control.head_object(bucket, normalized_key) or {})
        if (
            selected_headers.get("content-length") != str(size)
            or selected_headers.get("x-amz-meta-clientplatform-sha256") != sha256
            or selected_headers.get("x-amz-meta-clientplatform-size") != str(size)
        ):
            raise ReplicationError("backup_upload_verification_failed")
        return UploadedFile(key=normalized_key, size=size, sha256=sha256)
