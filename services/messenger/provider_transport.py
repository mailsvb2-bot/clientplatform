from __future__ import annotations

import http.client
import ipaddress
import json
import mimetypes
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, TypeVar
from uuid import uuid4

_T = TypeVar("_T")
_UPLOAD_CHUNK_SIZE = 256 * 1024
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan")


class ProviderPermanentHTTPError(RuntimeError):
    """A provider HTTP response that must not be retried by the HTTP client."""

    def __init__(self, status_code: int):
        self.status_code = int(status_code)
        super().__init__(f"provider_http_{self.status_code}")


class ProviderUploadURLRejected(RuntimeError):
    """An upload target that violates the outbound transport safety contract."""

    def __init__(self, code: str):
        self.code = str(code or "upload_url_rejected")
        super().__init__(self.code)


def _deployed_env() -> bool:
    return (os.getenv("APP_ENV") or "dev").strip().lower() in {
        "prod",
        "production",
        "stage",
        "staging",
    }


def _retry_count() -> int:
    raw = (os.getenv("MESSENGER_PROVIDER_RETRIES") or "3").strip()
    try:
        return min(max(1, int(raw)), 20)
    except ValueError:
        return 3


def _retry_backoff_sec(attempt: int) -> float:
    raw = (os.getenv("MESSENGER_PROVIDER_RETRY_BACKOFF_SEC") or "0.35").strip()
    try:
        base = float(raw)
    except ValueError:
        base = 0.35
    if not (base > 0 and base < float("inf")):
        base = 0.35
    return min(max(0.05, base) * (2 ** max(0, attempt - 1)), 3.0)


def _retryable_http_status(code: int) -> bool:
    return int(code) in {408, 429} or 500 <= int(code) <= 599


def _maybe_retry(attempt: int, max_attempts: int, exc: Exception) -> Exception | None:
    if attempt >= max_attempts:
        return exc
    time.sleep(_retry_backoff_sec(attempt))
    return None


def _with_retries(operation: Callable[[], _T], *, retries: int | None = None) -> _T:
    last_exc: Exception | None = None
    max_attempts = min(max(1, int(retries if retries is not None else _retry_count())), 20)
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except urllib.error.HTTPError as exc:
            if not _retryable_http_status(int(exc.code)):
                raise ProviderPermanentHTTPError(int(exc.code)) from exc
            last_exc = _maybe_retry(attempt, max_attempts, exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = _maybe_retry(attempt, max_attempts, exc)
        if last_exc is not None:
            break
    assert last_exc is not None
    raise last_exc


def json_request(
    url: str,
    *,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 20,
    retries: int | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> dict[str, Any]:
    data = None
    req_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    def _request() -> dict[str, Any]:
        request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    return _with_retries(_request, retries=retries)


def form_request(
    url: str,
    params: dict[str, Any],
    *,
    timeout: float = 20,
    retries: int | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> dict[str, Any]:
    encoded = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode("utf-8")

    def _request() -> dict[str, Any]:
        request = urllib.request.Request(url, data=encoded, method="POST")
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    return _with_retries(_request, retries=retries)


def _public_ip(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def validate_provider_upload_url(url: str) -> urllib.parse.SplitResult:
    """Reject credentials, cleartext and local/private upload destinations."""

    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme.lower() != "https":
        raise ProviderUploadURLRejected("upload_url_https_required")
    if parsed.username or parsed.password:
        raise ProviderUploadURLRejected("upload_url_credentials_forbidden")
    host = str(parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise ProviderUploadURLRejected("upload_url_host_required")
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise ProviderUploadURLRejected("upload_url_local_host_forbidden")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ProviderUploadURLRejected("upload_url_private_ip_forbidden")

    if _deployed_env() and literal is None:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            }
        except OSError as exc:
            raise ProviderUploadURLRejected("upload_url_dns_resolution_failed") from exc
        if not addresses or any(not _public_ip(address) for address in addresses):
            raise ProviderUploadURLRejected("upload_url_non_public_dns_target")
    return parsed


def multipart_bytes(field_name: str, filename: str, content: bytes, *, content_type: str) -> tuple[bytes, str]:
    boundary = f"----ClientPlatformBoundary{uuid4().hex}"
    head = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"{field_name}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head + content + tail, boundary


def _multipart_parts(field_name: str, filename: str, *, content_type: str) -> tuple[bytes, bytes, str]:
    boundary = f"----ClientPlatformBoundary{uuid4().hex}"
    safe_filename = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    safe_field = field_name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    head = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"{safe_field}\"; filename=\"{safe_filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head, tail, boundary


def _stream_multipart_once(
    url: str,
    *,
    token: str | None,
    field_name: str,
    path: Path,
    timeout: float,
    ssl_context: ssl.SSLContext | None,
) -> dict[str, Any]:
    parsed = validate_provider_upload_url(url)
    host = parsed.hostname
    if host is None:
        raise ProviderUploadURLRejected("upload_url_host_required")
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    head, tail, boundary = _multipart_parts(field_name, path.name, content_type=mime_type)
    file_size = path.stat().st_size
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    connection = http.client.HTTPSConnection(
        host,
        parsed.port or 443,
        timeout=timeout,
        context=ssl_context,
    )
    try:
        connection.putrequest("POST", target)
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(len(head) + file_size + len(tail)))
        if token:
            connection.putheader("Authorization", token)
        connection.endheaders()
        connection.send(head)
        with path.open("rb") as source:
            while chunk := source.read(_UPLOAD_CHUNK_SIZE):
                connection.send(chunk)
        connection.send(tail)
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        status = int(response.status)
        if not 200 <= status <= 299:
            raise urllib.error.HTTPError(url, status, response.reason, response.headers, None)
        return json.loads(raw) if raw else {}
    finally:
        connection.close()


def multipart_upload(
    url: str,
    *,
    token: str | None = None,
    field_name: str,
    path: Path,
    timeout: float = 120,
    retries: int | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)

    def _request() -> dict[str, Any]:
        return _stream_multipart_once(
            url,
            token=token,
            field_name=field_name,
            path=source,
            timeout=timeout,
            ssl_context=ssl_context,
        )

    return _with_retries(_request, retries=retries)
