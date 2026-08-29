from __future__ import annotations

import asyncio
import os
from typing import Any

from aiohttp import web

from clientplatform.application.external_products import ingest_external_product_webhook
from clientplatform.domain.external_products import (
    ExternalProductInvariantViolation,
    ExternalProductNotFound,
    ExternalProductSignatureError,
)


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_MAX_BODY_BYTES = 64 * 1024
_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}


def external_product_ingress_enabled() -> bool:
    raw = str(os.getenv("CLIENTPLATFORM_EXTERNAL_PRODUCT_INGRESS_ENABLED") or "")
    return raw.strip().lower() in _TRUE_VALUES


async def external_product_event_webhook(request: web.Request) -> web.Response:
    raw_length = request.headers.get("Content-Length")
    if raw_length:
        try:
            declared = int(raw_length)
        except ValueError:
            return _response(400, "invalid_content_length")
        if declared < 1 or declared > _MAX_BODY_BYTES:
            return _response(413, "body_too_large")
    try:
        body = await request.read()
    except (OSError, ValueError):
        return _response(400, "body_unavailable")
    if not body:
        return _response(400, "body_empty")
    if len(body) > _MAX_BODY_BYTES:
        return _response(413, "body_too_large")
    try:
        receipt = await asyncio.to_thread(
            ingest_external_product_webhook,
            connector_id=str(request.match_info.get("connector_id") or ""),
            timestamp_header=str(
                request.headers.get("X-ClientPlatform-Timestamp") or ""
            ),
            signature_header=str(
                request.headers.get("X-ClientPlatform-Signature") or ""
            ),
            body=body,
        )
    except ExternalProductNotFound:
        return _response(404, "connector_not_found")
    except ExternalProductSignatureError:
        return _response(401, "signature_rejected")
    except ExternalProductInvariantViolation:
        return _response(409, "event_conflict")
    except (TypeError, ValueError):
        return _response(400, "event_invalid")
    return web.json_response(
        {
            "ok": True,
            "receipt_id": receipt.id,
            "event_id": receipt.external_event_id,
            "outcome_recorded": receipt.outcome_event_id is not None,
        },
        headers=_RESPONSE_HEADERS,
    )


def _response(status: int, code: str) -> web.Response:
    return web.json_response(
        {"ok": False, "error": str(code)},
        status=int(status),
        headers=_RESPONSE_HEADERS,
    )


__all__ = ["external_product_event_webhook", "external_product_ingress_enabled"]
