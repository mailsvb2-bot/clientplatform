from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable

from aiohttp import web


_WEBHOOK_PREFIXES = (
    "/clientplatform/webhooks/vk/",
    "/clientplatform/webhooks/max/",
)
_SETUP_PREFIX = "/clientplatform/connect/"
_EXTERNAL_PRODUCT_PREFIX = "/clientplatform/external-products/"
_webhook_slots: asyncio.Semaphore | None = None
_webhook_slots_size = 0
_setup_slots: asyncio.Semaphore | None = None
_setup_slots_size = 0
_external_product_slots: asyncio.Semaphore | None = None
_external_product_slots_size = 0


def _positive_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(os.getenv(name) or default).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def native_webhook_body_limit() -> int:
    return _positive_int(
        "CLIENTPLATFORM_NATIVE_WEBHOOK_MAX_BODY_BYTES",
        262_144,
        minimum=4096,
        maximum=1024 * 1024,
    )


def native_setup_body_limit() -> int:
    return _positive_int(
        "CLIENTPLATFORM_NATIVE_SETUP_MAX_BODY_BYTES",
        8192,
        minimum=4096,
        maximum=64 * 1024,
    )


def external_product_body_limit() -> int:
    return _positive_int(
        "CLIENTPLATFORM_EXTERNAL_PRODUCT_MAX_BODY_BYTES",
        64 * 1024,
        minimum=4096,
        maximum=64 * 1024,
    )


def _request_kind(request: web.Request) -> str | None:
    if request.method != "POST":
        return None
    if request.path.startswith(_WEBHOOK_PREFIXES):
        return "webhook"
    if request.path.startswith(_SETUP_PREFIX):
        return "setup"
    if request.path.startswith(_EXTERNAL_PRODUCT_PREFIX):
        return "external_product"
    return None


def _slots(kind: str) -> asyncio.Semaphore:
    global _webhook_slots, _webhook_slots_size
    global _setup_slots, _setup_slots_size
    global _external_product_slots, _external_product_slots_size

    if kind == "webhook":
        size = _positive_int(
            "CLIENTPLATFORM_NATIVE_WEBHOOK_MAX_INFLIGHT",
            32,
            minimum=1,
            maximum=256,
        )
        if _webhook_slots is None or _webhook_slots_size != size:
            _webhook_slots = asyncio.Semaphore(size)
            _webhook_slots_size = size
        return _webhook_slots

    if kind == "external_product":
        size = _positive_int(
            "CLIENTPLATFORM_EXTERNAL_PRODUCT_MAX_INFLIGHT",
            16,
            minimum=1,
            maximum=128,
        )
        if (
            _external_product_slots is None
            or _external_product_slots_size != size
        ):
            _external_product_slots = asyncio.Semaphore(size)
            _external_product_slots_size = size
        return _external_product_slots

    size = _positive_int(
        "CLIENTPLATFORM_NATIVE_SETUP_MAX_INFLIGHT",
        4,
        minimum=1,
        maximum=32,
    )
    if _setup_slots is None or _setup_slots_size != size:
        _setup_slots = asyncio.Semaphore(size)
        _setup_slots_size = size
    return _setup_slots


async def _acquire(kind: str) -> asyncio.Semaphore | None:
    semaphore = _slots(kind)
    timeout_ms = _positive_int(
        "CLIENTPLATFORM_NATIVE_HTTP_QUEUE_TIMEOUT_MS",
        100,
        minimum=1,
        maximum=10_000,
    )
    try:
        await asyncio.wait_for(
            semaphore.acquire(),
            timeout=float(timeout_ms) / 1000.0,
        )
    except asyncio.TimeoutError:
        return None
    return semaphore


def _rejected(*, status: int, text: str, retry_after: str | None = None) -> web.Response:
    headers = {
        "Cache-Control": "no-store, max-age=0",
        "X-Content-Type-Options": "nosniff",
    }
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return web.Response(status=status, text=text, headers=headers)


@web.middleware
async def native_messenger_http_admission_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Bound unauthenticated work before vault and provider API boundaries."""

    kind = _request_kind(request)
    if kind is None:
        return await handler(request)

    if kind == "webhook":
        body_limit = native_webhook_body_limit()
    elif kind == "external_product":
        body_limit = external_product_body_limit()
    else:
        body_limit = native_setup_body_limit()
    content_length = request.content_length
    if content_length is not None and int(content_length) > body_limit:
        return _rejected(status=413, text="payload_too_large")
    try:
        raw_body = await request.read()
    except (ValueError, OSError):
        return _rejected(status=400, text="body_read_failed")
    if len(raw_body) > body_limit:
        return _rejected(status=413, text="payload_too_large")

    semaphore = await _acquire(kind)
    if semaphore is None:
        return _rejected(status=429, text="busy", retry_after="1")
    try:
        return await handler(request)
    finally:
        semaphore.release()


def reset_native_messenger_http_admission_state_for_tests() -> None:
    global _webhook_slots, _webhook_slots_size
    global _setup_slots, _setup_slots_size
    global _external_product_slots, _external_product_slots_size
    _webhook_slots = None
    _webhook_slots_size = 0
    _setup_slots = None
    _setup_slots_size = 0
    _external_product_slots = None
    _external_product_slots_size = 0


__all__ = [
    "external_product_body_limit",
    "native_messenger_http_admission_middleware",
    "native_setup_body_limit",
    "native_webhook_body_limit",
    "reset_native_messenger_http_admission_state_for_tests",
]
