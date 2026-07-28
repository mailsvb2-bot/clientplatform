from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import mimetypes
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from a1.runtime.secrets import EnvironmentCredentialProvider, SecretReferenceError
from a1.transport.media import (
    MediaReferenceError,
    parse_s3_reference,
    verify_media_gateway_signature,
)
from core.runtime_env import env_float, env_int

log = logging.getLogger(__name__)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class MediaObjectNotFound(RuntimeError):
    pass


class MediaObjectUnavailable(RuntimeError):
    pass


class MediaObjectTooLarge(RuntimeError):
    pass


class MediaRangeNotSatisfiable(RuntimeError):
    pass


class MediaObject(Protocol):
    status: int
    content_type: str
    content_length: int | None
    content_range: str

    async def read(self, size: int) -> bytes: ...

    async def close(self) -> None: ...


class MediaObjectStore(Protocol):
    async def open(self, *, bucket: str, key: str, range_header: str = "") -> MediaObject: ...


@dataclass(frozen=True, slots=True)
class MediaGatewayConfig:
    enabled: bool
    host: str
    port: int
    public_base_url: str
    storage_mode: str
    allowed_buckets: frozenset[str]
    filesystem_root: str
    s3_endpoint: str
    s3_region: str
    s3_access_key_reference: str
    s3_secret_key_reference: str
    s3_session_token_reference: str
    signing_secret_reference: str
    max_object_bytes: int
    upstream_timeout_seconds: float
    chunk_size: int

    @property
    def route_prefix(self) -> str:
        if not self.public_base_url:
            return ""
        return urlsplit(self.public_base_url).path.rstrip("/")


@dataclass(slots=True)
class MediaGatewayHealth:
    configured: bool = False
    running: bool = False
    requests: int = 0
    denied: int = 0
    not_found: int = 0
    upstream_errors: int = 0
    bytes_streamed: int = 0
    last_error: str = ""


@dataclass(slots=True)
class MediaGatewayRuntime:
    runner: Any
    site: Any
    config: MediaGatewayConfig

    async def stop(self) -> None:
        await self.runner.cleanup()


class _FilesystemMediaObject:
    def __init__(
        self,
        *,
        handle: Any,
        status: int,
        content_type: str,
        content_length: int,
        content_range: str,
        remaining: int,
    ) -> None:
        self._handle = handle
        self.status = status
        self.content_type = content_type
        self.content_length = content_length
        self.content_range = content_range
        self._remaining = remaining

    async def read(self, size: int) -> bytes:
        if self._remaining <= 0:
            return b""
        amount = min(max(1, int(size)), self._remaining)
        data = await asyncio.to_thread(self._handle.read, amount)
        self._remaining -= len(data)
        return data

    async def close(self) -> None:
        await asyncio.to_thread(self._handle.close)


class FilesystemMediaObjectStore:
    def __init__(self, *, root: str, max_object_bytes: int) -> None:
        selected = Path(str(root or "")).expanduser()
        if not selected.is_absolute():
            raise ValueError("media filesystem root must be absolute")
        self._root = selected.resolve()
        self._max_object_bytes = max(1, int(max_object_bytes))

    async def open(self, *, bucket: str, key: str, range_header: str = "") -> MediaObject:
        normalized_bucket, normalized_key = parse_s3_reference(f"s3://{bucket}/{key}")
        bucket_root = (self._root / normalized_bucket).resolve()
        selected = (bucket_root / normalized_key).resolve()
        try:
            selected.relative_to(bucket_root)
        except ValueError:
            raise MediaObjectNotFound("media_object_not_found") from None
        try:
            stat = await asyncio.to_thread(selected.stat)
        except FileNotFoundError:
            raise MediaObjectNotFound("media_object_not_found") from None
        except OSError:
            raise MediaObjectUnavailable("media_object_unavailable") from None
        if not selected.is_file():
            raise MediaObjectNotFound("media_object_not_found")
        total_size = int(stat.st_size)
        if total_size > self._max_object_bytes:
            raise MediaObjectTooLarge("media_object_too_large")
        start, end, status = _resolve_byte_range(range_header, total_size)
        content_length = max(0, end - start + 1)
        try:
            handle = await asyncio.to_thread(selected.open, "rb")
            await asyncio.to_thread(handle.seek, start)
        except OSError:
            raise MediaObjectUnavailable("media_object_unavailable") from None
        content_type = mimetypes.guess_type(selected.name)[0] or "application/octet-stream"
        content_range = f"bytes {start}-{end}/{total_size}" if status == 206 else ""
        return _FilesystemMediaObject(
            handle=handle,
            status=status,
            content_type=content_type,
            content_length=content_length,
            content_range=content_range,
            remaining=content_length,
        )


class _S3MediaObject:
    def __init__(
        self,
        *,
        session: Any,
        response: Any,
        status: int,
        content_type: str,
        content_length: int | None,
        content_range: str,
        max_object_bytes: int,
    ) -> None:
        self._session = session
        self._response = response
        self.status = status
        self.content_type = content_type
        self.content_length = content_length
        self.content_range = content_range
        self._max_object_bytes = max_object_bytes
        self._read = 0

    async def read(self, size: int) -> bytes:
        data = await self._response.content.read(max(1, int(size)))
        self._read += len(data)
        if self._read > self._max_object_bytes:
            raise MediaObjectTooLarge("media_object_too_large")
        return data

    async def close(self) -> None:
        self._response.release()
        await self._session.close()


class S3CompatibleMediaObjectStore:
    def __init__(
        self,
        *,
        endpoint: str,
        region: str,
        credential_provider: EnvironmentCredentialProvider,
        access_key_reference: str,
        secret_key_reference: str,
        session_token_reference: str = "",
        timeout_seconds: float = 30.0,
        max_object_bytes: int = 262_144_000,
        clock: Any = time.time,
    ) -> None:
        parsed = urlsplit(str(endpoint or "").strip().rstrip("/"))
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("S3 endpoint must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("S3 endpoint must not contain credentials or query")
        normalized_region = str(region or "").strip()
        if not normalized_region or len(normalized_region) > 64:
            raise ValueError("S3 region is required")
        self._endpoint = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        self._endpoint_path = parsed.path.rstrip("/")
        self._host = parsed.netloc
        self._region = normalized_region
        self._credential_provider = credential_provider
        self._access_key_reference = str(access_key_reference or "").strip()
        self._secret_key_reference = str(secret_key_reference or "").strip()
        self._session_token_reference = str(session_token_reference or "").strip()
        if not self._access_key_reference or not self._secret_key_reference:
            raise ValueError("S3 credential references are required")
        timeout = float(timeout_seconds)
        if timeout <= 0 or timeout > 120:
            raise ValueError("S3 timeout must be between 0 and 120 seconds")
        self._timeout_seconds = timeout
        self._max_object_bytes = max(1, int(max_object_bytes))
        self._clock = clock

    def _resolve_credentials(self) -> tuple[str, str, str]:
        access_key = self._credential_provider.resolve(self._access_key_reference)
        secret_key = self._credential_provider.resolve(self._secret_key_reference)
        session_token = ""
        if self._session_token_reference:
            session_token = self._credential_provider.resolve(self._session_token_reference)
        return access_key, secret_key, session_token

    async def open(self, *, bucket: str, key: str, range_header: str = "") -> MediaObject:
        normalized_bucket, normalized_key = parse_s3_reference(f"s3://{bucket}/{key}")
        try:
            access_key, secret_key, session_token = await asyncio.to_thread(
                self._resolve_credentials
            )
        except SecretReferenceError:
            raise MediaObjectUnavailable("media_storage_credentials_unavailable") from None
        headers, canonical_path = _s3_authorization_headers(
            host=self._host,
            endpoint_path=self._endpoint_path,
            bucket=normalized_bucket,
            key=normalized_key,
            region=self._region,
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            range_header=range_header,
            clock=self._clock,
        )
        url = f"{self._endpoint}{canonical_path[len(self._endpoint_path):]}"
        try:
            import aiohttp
        except ImportError:
            raise MediaObjectUnavailable("media_http_dependency_missing") from None
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        session = aiohttp.ClientSession(timeout=timeout)
        try:
            response = await session.get(url, headers=headers, allow_redirects=False)
        except asyncio.TimeoutError:
            await session.close()
            raise MediaObjectUnavailable("media_storage_timeout") from None
        except aiohttp.ClientError:
            await session.close()
            raise MediaObjectUnavailable("media_storage_transport_failure") from None
        status = int(response.status)
        if status == 404:
            response.release()
            await session.close()
            raise MediaObjectNotFound("media_object_not_found")
        if status == 416:
            response.release()
            await session.close()
            raise MediaRangeNotSatisfiable("media_range_not_satisfiable")
        if status not in {200, 206}:
            response.release()
            await session.close()
            raise MediaObjectUnavailable("media_storage_upstream_failure")
        content_length = _optional_content_length(response.headers.get("Content-Length"))
        if content_length is not None and content_length > self._max_object_bytes:
            response.release()
            await session.close()
            raise MediaObjectTooLarge("media_object_too_large")
        return _S3MediaObject(
            session=session,
            response=response,
            status=status,
            content_type=str(
                response.headers.get("Content-Type") or "application/octet-stream"
            ).split(";", 1)[0].strip(),
            content_length=content_length,
            content_range=str(response.headers.get("Content-Range") or "").strip(),
            max_object_bytes=self._max_object_bytes,
        )


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() in _TRUE_VALUES


def media_gateway_configured() -> bool:
    return _env_bool("A1_MEDIA_GATEWAY_ENABLED", False)


def _allowed_buckets(raw: str) -> frozenset[str]:
    values: set[str] = set()
    for item in str(raw or "").split(","):
        normalized = item.strip().lower()
        if not normalized:
            continue
        bucket, _ = parse_s3_reference(f"s3://{normalized}/validation")
        values.add(bucket)
    return frozenset(values)


def media_gateway_config() -> MediaGatewayConfig:
    return MediaGatewayConfig(
        enabled=media_gateway_configured(),
        host=str(os.getenv("A1_MEDIA_GATEWAY_HOST") or "127.0.0.1").strip(),
        port=env_int(
            "A1_MEDIA_GATEWAY_PORT",
            8091,
            minimum=1,
            maximum=65_535,
        ),
        public_base_url=str(os.getenv("A1_MEDIA_GATEWAY_BASE_URL") or "").strip().rstrip("/"),
        storage_mode=str(os.getenv("A1_MEDIA_GATEWAY_STORAGE_MODE") or "").strip().lower(),
        allowed_buckets=_allowed_buckets(
            str(os.getenv("A1_MEDIA_GATEWAY_ALLOWED_BUCKETS") or "")
        ),
        filesystem_root=str(os.getenv("A1_MEDIA_GATEWAY_FILESYSTEM_ROOT") or "").strip(),
        s3_endpoint=str(os.getenv("A1_MEDIA_GATEWAY_S3_ENDPOINT") or "").strip().rstrip("/"),
        s3_region=str(os.getenv("A1_MEDIA_GATEWAY_S3_REGION") or "").strip(),
        s3_access_key_reference=str(
            os.getenv("A1_MEDIA_GATEWAY_S3_ACCESS_KEY_REFERENCE")
            or "secret://env/A1_SECRET_S3_ACCESS_KEY"
        ).strip(),
        s3_secret_key_reference=str(
            os.getenv("A1_MEDIA_GATEWAY_S3_SECRET_KEY_REFERENCE")
            or "secret://env/A1_SECRET_S3_SECRET_KEY"
        ).strip(),
        s3_session_token_reference=str(
            os.getenv("A1_MEDIA_GATEWAY_S3_SESSION_TOKEN_REFERENCE") or ""
        ).strip(),
        signing_secret_reference=str(
            os.getenv("A1_MEDIA_SIGNING_SECRET_REFERENCE")
            or "secret://env/A1_SECRET_MEDIA_SIGNING_KEY"
        ).strip(),
        max_object_bytes=env_int(
            "A1_MEDIA_GATEWAY_MAX_OBJECT_BYTES",
            262_144_000,
            minimum=1_048_576,
            maximum=2_147_483_647,
        ),
        upstream_timeout_seconds=env_float(
            "A1_MEDIA_GATEWAY_UPSTREAM_TIMEOUT_SEC",
            30.0,
            minimum=1.0,
            maximum=120.0,
        ),
        chunk_size=env_int(
            "A1_MEDIA_GATEWAY_CHUNK_SIZE",
            65_536,
            minimum=4_096,
            maximum=1_048_576,
        ),
    )


def validate_media_gateway_config(config: MediaGatewayConfig) -> None:
    if not config.enabled:
        return
    parsed = urlsplit(config.public_base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("media gateway public base URL must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("media gateway public base URL is invalid")
    if not config.host or any(ord(char) < 32 for char in config.host):
        raise ValueError("media gateway host is invalid")
    if not config.allowed_buckets:
        raise ValueError("media gateway allowed buckets are required")
    if config.storage_mode not in {"filesystem", "s3"}:
        raise ValueError("media gateway storage mode must be filesystem or s3")
    if config.storage_mode == "filesystem":
        selected = Path(config.filesystem_root).expanduser()
        if not selected.is_absolute():
            raise ValueError("media gateway filesystem root must be absolute")
    if config.storage_mode == "s3":
        if not config.s3_endpoint or not config.s3_region:
            raise ValueError("media gateway S3 endpoint and region are required")


def build_media_object_store(
    config: MediaGatewayConfig,
    credential_provider: EnvironmentCredentialProvider,
) -> MediaObjectStore:
    validate_media_gateway_config(config)
    if config.storage_mode == "filesystem":
        return FilesystemMediaObjectStore(
            root=config.filesystem_root,
            max_object_bytes=config.max_object_bytes,
        )
    return S3CompatibleMediaObjectStore(
        endpoint=config.s3_endpoint,
        region=config.s3_region,
        credential_provider=credential_provider,
        access_key_reference=config.s3_access_key_reference,
        secret_key_reference=config.s3_secret_key_reference,
        session_token_reference=config.s3_session_token_reference,
        timeout_seconds=config.upstream_timeout_seconds,
        max_object_bytes=config.max_object_bytes,
    )


def _optional_content_length(value: Any) -> int | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed = int(normalized)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _resolve_byte_range(header: str, size: int) -> tuple[int, int, int]:
    if size < 0:
        raise MediaRangeNotSatisfiable("media_range_not_satisfiable")
    normalized = str(header or "").strip()
    if not normalized:
        if size == 0:
            return 0, -1, 200
        return 0, size - 1, 200
    if not normalized.startswith("bytes=") or "," in normalized:
        raise MediaRangeNotSatisfiable("media_range_not_satisfiable")
    spec = normalized[6:].strip()
    start_raw, separator, end_raw = spec.partition("-")
    if not separator:
        raise MediaRangeNotSatisfiable("media_range_not_satisfiable")
    try:
        if start_raw:
            start = int(start_raw)
            end = int(end_raw) if end_raw else size - 1
        else:
            suffix = int(end_raw)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
    except ValueError:
        raise MediaRangeNotSatisfiable("media_range_not_satisfiable") from None
    if size <= 0 or start < 0 or end < start or start >= size:
        raise MediaRangeNotSatisfiable("media_range_not_satisfiable")
    return start, min(end, size - 1), 206


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    date_key = hmac.new(
        f"AWS4{secret_key}".encode("utf-8"),
        date_stamp.encode("ascii"),
        hashlib.sha256,
    ).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _s3_authorization_headers(
    *,
    host: str,
    endpoint_path: str,
    bucket: str,
    key: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
    range_header: str,
    clock: Any,
) -> tuple[dict[str, str], str]:
    timestamp = datetime.fromtimestamp(float(clock()), tz=timezone.utc)
    amz_date = timestamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = timestamp.strftime("%Y%m%d")
    canonical_path = (
        f"{endpoint_path}/{quote(bucket, safe='-_.~')}/"
        f"{quote(key, safe='/-_.~')}"
    )
    headers: dict[str, str] = {
        "host": host,
        "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
        "x-amz-date": amz_date,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    normalized_range = str(range_header or "").strip()
    if normalized_range:
        headers["range"] = normalized_range
    signed_names = sorted(headers)
    canonical_headers = "".join(f"{name}:{headers[name].strip()}\n" for name in signed_names)
    signed_headers = ";".join(signed_names)
    canonical_request = "\n".join(
        [
            "GET",
            canonical_path,
            "",
            canonical_headers,
            signed_headers,
            "UNSIGNED-PAYLOAD",
        ]
    )
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    outbound = {name.title(): value for name, value in headers.items()}
    outbound["Authorization"] = authorization
    return outbound, canonical_path


_health = MediaGatewayHealth()
_runtime: MediaGatewayRuntime | None = None


def media_gateway_health_snapshot() -> dict[str, Any]:
    return {
        "a1_media_gateway_configured": media_gateway_configured(),
        "a1_media_gateway_health_available": True,
        "a1_media_gateway_running": _health.running,
        "a1_media_gateway_requests": _health.requests,
        "a1_media_gateway_denied": _health.denied,
        "a1_media_gateway_not_found": _health.not_found,
        "a1_media_gateway_upstream_errors": _health.upstream_errors,
        "a1_media_gateway_bytes_streamed": _health.bytes_streamed,
        "a1_media_gateway_last_error": _health.last_error,
    }


async def _media_response(
    request: Any,
    *,
    config: MediaGatewayConfig,
    store: MediaObjectStore,
    credential_provider: EnvironmentCredentialProvider,
) -> Any:
    try:
        from aiohttp import web
    except ImportError:
        raise MediaObjectUnavailable("media_http_dependency_missing") from None

    _health.requests += 1
    raw_path = str(request.raw_path or "").partition("?")[0]
    expires_values = request.query.getall("expires", [])
    signature_values = request.query.getall("sig", [])
    if set(request.query) != {"expires", "sig"} or len(expires_values) != 1 or len(signature_values) != 1:
        _health.denied += 1
        return web.Response(status=403)
    try:
        secret = await asyncio.to_thread(
            credential_provider.resolve,
            config.signing_secret_reference,
        )
        verify_media_gateway_signature(
            secret=secret,
            path=raw_path,
            expires=expires_values[0],
            signature=signature_values[0],
        )
        bucket, key = parse_s3_reference(
            f"s3://{request.match_info.get('bucket', '')}/{request.match_info.get('key', '')}"
        )
        if bucket not in config.allowed_buckets:
            raise MediaReferenceError("media_storage_bucket_not_allowed")
    except MediaReferenceError:
        _health.denied += 1
        return web.Response(status=403)
    except SecretReferenceError:
        _health.upstream_errors += 1
        _health.last_error = "SecretReferenceError"
        return web.Response(status=503)

    media: MediaObject | None = None
    try:
        media = await store.open(
            bucket=bucket,
            key=key,
            range_header=str(request.headers.get("Range") or ""),
        )
        headers = {
            "Content-Type": media.content_type,
            "Cache-Control": "private, no-store",
            "Accept-Ranges": "bytes",
            "X-Content-Type-Options": "nosniff",
        }
        if media.content_length is not None:
            headers["Content-Length"] = str(media.content_length)
        if media.content_range:
            headers["Content-Range"] = media.content_range
        if request.method == "HEAD":
            return web.Response(status=media.status, headers=headers)
        response = web.StreamResponse(status=media.status, headers=headers)
        await response.prepare(request)
        while True:
            chunk = await media.read(config.chunk_size)
            if not chunk:
                break
            _health.bytes_streamed += len(chunk)
            await response.write(chunk)
        await response.write_eof()
        _health.last_error = ""
        return response
    except MediaObjectNotFound:
        _health.not_found += 1
        return web.Response(status=404)
    except MediaRangeNotSatisfiable:
        _health.denied += 1
        return web.Response(status=416)
    except MediaObjectTooLarge:
        _health.denied += 1
        return web.Response(status=413)
    except MediaObjectUnavailable as exc:
        _health.upstream_errors += 1
        _health.last_error = type(exc).__name__
        return web.Response(status=503)
    except OSError as exc:
        _health.upstream_errors += 1
        _health.last_error = type(exc).__name__
        return web.Response(status=503)
    finally:
        if media is not None:
            await media.close()


async def start_media_gateway_runtime(
    config: MediaGatewayConfig | None = None,
    *,
    store: MediaObjectStore | None = None,
    credential_provider: EnvironmentCredentialProvider | None = None,
) -> MediaGatewayRuntime | None:
    global _runtime
    selected = config or media_gateway_config()
    if not selected.enabled:
        return None
    if _runtime is not None:
        return _runtime
    validate_media_gateway_config(selected)
    provider = credential_provider or EnvironmentCredentialProvider()
    selected_store = store or build_media_object_store(selected, provider)
    try:
        from aiohttp import web
    except ImportError:
        raise RuntimeError("media_gateway_http_dependency_missing") from None
    app = web.Application(client_max_size=1024)

    async def handler(request: Any) -> Any:
        return await _media_response(
            request,
            config=selected,
            store=selected_store,
            credential_provider=provider,
        )

    route = f"{selected.route_prefix}/media/{{bucket}}/{{key:.+}}"
    app.router.add_get(route, handler, allow_head=True)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, selected.host, selected.port)
        await site.start()
    except OSError:
        await runner.cleanup()
        raise
    runtime = MediaGatewayRuntime(runner=runner, site=site, config=selected)
    _runtime = runtime
    _health.configured = True
    _health.running = True
    _health.last_error = ""
    log.info("A1 media gateway started on %s:%s", selected.host, selected.port)
    return runtime


async def stop_media_gateway_runtime() -> None:
    global _runtime
    runtime = _runtime
    _runtime = None
    if runtime is not None:
        await runtime.stop()
    _health.running = False


async def run_media_gateway_owner(
    *,
    config: MediaGatewayConfig | None = None,
    store: MediaObjectStore | None = None,
    credential_provider: EnvironmentCredentialProvider | None = None,
) -> None:
    selected = config or media_gateway_config()
    if not selected.enabled:
        return
    runtime = await start_media_gateway_runtime(
        selected,
        store=store,
        credential_provider=credential_provider,
    )
    if runtime is None:
        return
    try:
        await asyncio.Event().wait()
    finally:
        await stop_media_gateway_runtime()
        log.info("A1 media gateway stopped")
