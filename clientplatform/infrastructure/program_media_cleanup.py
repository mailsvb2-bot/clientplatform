from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from clientplatform.domain.program_media import unwrap_program_media_reference
from clientplatform.infrastructure.program_media_store import (
    ProgramMediaStoreError,
    _authorization_headers,
    _EMPTY_SHA256,
    program_media_store_config,
)


def _owned_object(reference: str, *, bucket: str) -> tuple[str, str]:
    normalized = unwrap_program_media_reference(reference)
    parsed = urlsplit(normalized)
    if parsed.scheme != "s3" or parsed.netloc.lower() != bucket:
        raise ProgramMediaStoreError("program_media_cleanup_reference_invalid")
    if parsed.query or parsed.fragment:
        raise ProgramMediaStoreError("program_media_cleanup_reference_invalid")
    key = parsed.path.lstrip("/")
    segments = key.split("/")
    if (
        not key.startswith("program-media/")
        or len(key) > 1024
        or any(segment in {"", ".", ".."} for segment in segments)
        or any(ord(char) < 32 for char in key)
    ):
        raise ProgramMediaStoreError("program_media_cleanup_reference_invalid")
    return normalized, key


def delete_program_media_reference(
    reference: str,
    *,
    env: Mapping[str, str] | None = None,
    opener: Callable[..., Any] = urlopen,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> bool:
    values = os.environ if env is None else env
    config = program_media_store_config(values)
    if not config.enabled:
        raise ProgramMediaStoreError("program_media_ingest_disabled")
    _normalized, key = _owned_object(reference, bucket=config.bucket)
    request_path = f"{config.endpoint_path}/{config.bucket}/{key}" or "/"
    url = urlunsplit(
        (
            "https",
            config.endpoint_host,
            quote(request_path, safe="/-_.~%"),
            "",
            "",
        )
    )
    headers = _authorization_headers(
        method="DELETE",
        host=config.endpoint_host,
        path=request_path,
        region=config.region,
        access_key=config.access_key,
        secret_key=config.secret_key,
        session_token=config.session_token,
        payload_sha256=_EMPTY_SHA256,
        extra_headers={},
        now=clock(),
    )
    try:
        with opener(
            Request(url, headers=headers, method="DELETE"),
            timeout=config.timeout_seconds,
        ) as response:
            status = int(response.status)
            if status not in {200, 202, 204}:
                raise ProgramMediaStoreError(
                    "program_media_cleanup_status_invalid",
                    retryable=status >= 500 or status == 429,
                )
            response.read(65_536)
            return True
    except HTTPError as exc:
        exc.read(65_536)
        if exc.code == 404:
            return True
        raise ProgramMediaStoreError(
            "program_media_cleanup_http_failure",
            retryable=exc.code >= 500 or exc.code == 429,
        ) from None
    except (URLError, TimeoutError, OSError):
        raise ProgramMediaStoreError(
            "program_media_cleanup_transport_failure",
            retryable=True,
        ) from None


__all__ = ["delete_program_media_reference"]
