from __future__ import annotations

import asyncio
import ipaddress
import os
import time
from collections import deque
from typing import Awaitable, Callable

from aiohttp import web

_PAYMENT_WEBHOOK_PATH = "/pay/yookassa/webhook"
_rate_windows: dict[str, deque[float]] = {}
_verification_slots: asyncio.Semaphore | None = None
_verification_slots_size: int = 0


def _positive_int(name: str, default: int, *, minimum: int = 1, maximum: int = 100_000) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return min(max(int(value), int(minimum)), int(maximum))


def payment_webhook_body_limit() -> int:
    return _positive_int("PAYMENT_WEBHOOK_MAX_BODY_BYTES", 64 * 1024, minimum=1024, maximum=1024 * 1024)


def ingress_body_limit() -> int:
    configured = _positive_int("HTTP_INGRESS_MAX_BODY_BYTES", 1024 * 1024, minimum=4096, maximum=10 * 1024 * 1024)
    return max(configured, payment_webhook_body_limit())


def _trust_proxy_headers() -> bool:
    return (os.getenv("TRUST_PROXY_HEADERS") or "").strip().lower() in {"1", "true", "yes", "on"}


def _ip_address(raw: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw = (os.getenv("PAYMENT_WEBHOOK_TRUSTED_PROXY_CIDRS") or "").strip()
    if not raw:
        return ()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _proxy_headers_allowed(request: web.Request) -> bool:
    remote = _ip_address(request.remote)
    if remote is None:
        return False
    if remote.is_loopback:
        return True
    networks = _trusted_proxy_networks()
    if networks:
        return any(remote in network for network in networks if remote.version == network.version)
    # A global trust flag without an address allowlist lets any public peer spoof
    # X-Forwarded-For and rotate around the webhook rate limit. Loopback remains
    # trusted for the common local reverse-proxy deployment.
    return False


def _forwarded_client_address(request: web.Request) -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    candidate = _ip_address(forwarded)
    if candidate is None:
        candidate = _ip_address(request.headers.get("X-Real-IP"))
    return str(candidate) if candidate is not None else ""


def _client_key(request: web.Request) -> str:
    if _proxy_headers_allowed(request):
        forwarded = _forwarded_client_address(request)
        if forwarded:
            return f"client:{forwarded}"[:128]
    remote = _ip_address(request.remote)
    return f"peer:{remote or 'unknown'}"[:128]


def _rate_allowed(client_key: str, *, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else float(now)
    window_sec = _positive_int("PAYMENT_WEBHOOK_RATE_WINDOW_SEC", 60, minimum=1, maximum=3600)
    max_requests = _positive_int("PAYMENT_WEBHOOK_RATE_LIMIT", 30, minimum=1, maximum=10_000)
    cutoff = current - float(window_sec)
    bucket = _rate_windows.setdefault(str(client_key), deque())
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    if len(bucket) >= max_requests:
        return False
    bucket.append(current)

    # Bound memory even under high-cardinality source addresses. Old empty
    # buckets are removed opportunistically; the hard cap drops oldest entries.
    if len(_rate_windows) > 4096:
        stale = [key for key, values in _rate_windows.items() if not values or values[-1] <= cutoff]
        for key in stale[:1024]:
            _rate_windows.pop(key, None)
        while len(_rate_windows) > 4096:
            _rate_windows.pop(next(iter(_rate_windows)))
    return True


def _semaphore() -> asyncio.Semaphore:
    global _verification_slots, _verification_slots_size
    size = _positive_int("PAYMENT_WEBHOOK_MAX_INFLIGHT", 4, minimum=1, maximum=64)
    if _verification_slots is None or _verification_slots_size != size:
        _verification_slots = asyncio.Semaphore(size)
        _verification_slots_size = size
    return _verification_slots


async def _acquire_verification_slot() -> asyncio.Semaphore | None:
    semaphore = _semaphore()
    timeout_ms = _positive_int("PAYMENT_WEBHOOK_QUEUE_TIMEOUT_MS", 100, minimum=1, maximum=10_000)
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=float(timeout_ms) / 1000.0)
    except asyncio.TimeoutError:
        return None
    return semaphore


@web.middleware
async def payment_webhook_admission_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Fail fast before a request can consume a provider-verification thread.

    Provider API verification remains the payment source of truth. This boundary
    only limits request body size, per-source request rate and global concurrent
    verification work so forged unique payment IDs cannot exhaust the event
    loop's thread pool or saturate the YooKassa API connection budget.
    """

    if request.path != _PAYMENT_WEBHOOK_PATH:
        return await handler(request)

    body_limit = payment_webhook_body_limit()
    content_length = request.content_length
    if content_length is not None and int(content_length) > body_limit:
        return web.json_response({"ok": False, "error": "payload_too_large"}, status=413)

    try:
        raw_body = await request.read()
    except (ValueError, OSError):
        return web.json_response({"ok": False, "error": "body_read_failed"}, status=400)
    if len(raw_body) > body_limit:
        return web.json_response({"ok": False, "error": "payload_too_large"}, status=413)

    if not _rate_allowed(_client_key(request)):
        return web.json_response(
            {"ok": False, "error": "rate_limited"},
            status=429,
            headers={"Retry-After": str(_positive_int("PAYMENT_WEBHOOK_RATE_WINDOW_SEC", 60))},
        )

    semaphore = await _acquire_verification_slot()
    if semaphore is None:
        return web.json_response(
            {"ok": False, "error": "verification_busy"},
            status=429,
            headers={"Retry-After": "1"},
        )
    try:
        return await handler(request)
    finally:
        semaphore.release()


def reset_payment_webhook_admission_state_for_tests() -> None:
    global _verification_slots, _verification_slots_size
    _rate_windows.clear()
    _verification_slots = None
    _verification_slots_size = 0
