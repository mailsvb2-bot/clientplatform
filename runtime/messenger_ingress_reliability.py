from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from aiohttp import web

from runtime import messenger_ingress as base_ingress
from runtime.messenger_payloads import extract_max_message, extract_vk_message, max_event_key
from runtime.messenger_senders import MaxBotSender
from services.db import atomic_db
from services.events import log_event
from services.messenger.clientplatform_entry import (
    handle_clientplatform_entry,
    parse_clientplatform_entry_command,
)
from services.messenger.delivery_outbox import persist_reply_bundle
from services.messenger.webhook_dedupe import (
    InboundFailureResult,
    claim_inbound_event,
    fail_inbound_event,
    record_inbound_failure,
)

log = logging.getLogger(__name__)


def _positive_int(name: str, default: int, *, minimum: int = 1, maximum: int = 100) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return min(max(value, minimum), maximum)


def extraction_max_attempts() -> int:
    return _positive_int("MESSENGER_WEBHOOK_EXTRACTION_MAX_ATTEMPTS", 5, minimum=1, maximum=100)


async def _record_extraction_failure(
    *,
    platform: str,
    event_key: str,
    payload: dict[str, Any],
    reason: str,
) -> InboundFailureResult:
    result = await asyncio.to_thread(
        record_inbound_failure,
        platform,
        event_key,
        payload,
        reason,
        max_attempts=extraction_max_attempts(),
    )
    log.warning(
        "%s webhook extraction failure recorded: event_key=%s attempts=%s retryable=%s dead_lettered=%s recorded=%s",
        platform.upper(),
        result.event_key,
        result.attempts,
        result.retryable,
        result.dead_lettered,
        result.recorded,
    )
    return result


def _payload_from_body(body: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _process_clientplatform_entry_and_persist(
    *,
    platform: str,
    event_key: str,
    event_type: str,
    payload: dict[str, Any],
    extracted: dict[str, Any],
    text: str,
) -> bool:
    if not claim_inbound_event(platform, event_key, payload):
        return False
    try:
        command = parse_clientplatform_entry_command(text, event_type=event_type)
        if command is None:
            raise ValueError("ClientPlatform entry command disappeared during processing")
        with atomic_db():
            canonical_user_id, replies = handle_clientplatform_entry(
                extracted["user_id"],
                platform=platform,
                external_user_id=extracted["external_user_id"],
                text=text,
                event_type=event_type,
                username=extracted["username"],
                display_name=extracted["display_name"],
                first_name=extracted["first_name"],
                event_key=event_key,
            )
            persist_reply_bundle(
                platform=platform,
                external_user_id=extracted["external_user_id"],
                canonical_user_id=int(canonical_user_id),
                event_key=event_key,
                replies=list(replies),
                action=f"clientplatform_{command.action}",
            )
            log_event(
                int(canonical_user_id),
                f"{platform}_clientplatform_entry",
                {"action": command.action, "text_len": len(text)},
            )
        return True
    except Exception as exc:  # validator: allow-wide-except
        fail_inbound_event(platform, event_key, payload, type(exc).__name__)
        raise


async def vk_webhook(request: web.Request) -> web.Response:
    """Add finite extraction retries around the canonical ClientPlatform VK entry."""

    payload = _payload_from_body(await request.text())
    if payload is None or not base_ingress._vk_secret_ok(payload):
        return await base_ingress.vk_webhook(request)

    event_type = str(payload.get("type") or "").strip()
    if event_type not in base_ingress.VK_PROCESSABLE_EVENT_TYPES:
        return await base_ingress.vk_webhook(request)

    extracted = extract_vk_message(payload)
    if extracted is None:
        if event_type == "message_event":
            await base_ingress._ack_vk_message_event(payload)
        event_key = base_ingress._vk_dedupe_key(payload)
        result = await _record_extraction_failure(
            platform="vk",
            event_key=event_key,
            payload=payload,
            reason=f"extraction_failed:event_type={event_type or 'unknown'}",
        )
        if result.retryable:
            return web.Response(status=503, text="retry")
        return web.Response(text="ok")

    entry_text = base_ingress._entry_start_text(str(extracted.get("text") or ""))
    command = parse_clientplatform_entry_command(entry_text, event_type=event_type)
    if command is None:
        entry_text = "start"

    if event_type == "message_event":
        await base_ingress._ack_vk_message_event(payload)
    event_key = base_ingress._vk_dedupe_key(payload)
    try:
        processed = await asyncio.to_thread(
            _process_clientplatform_entry_and_persist,
            platform="vk",
            event_key=event_key,
            event_type=event_type,
            payload=payload,
            extracted=extracted,
            text=entry_text,
        )
    except Exception:  # validator: allow-wide-except
        log.exception("VK ClientPlatform entry processing failed")
        return web.Response(status=503, text="retry")
    if not processed:
        log.info("VK ClientPlatform entry duplicate skipped")
    return web.Response(text="ok")


async def _ack_global_max_owner_callback(payload: dict[str, Any]) -> None:
    if str(payload.get("update_type") or "").strip() != "message_callback":
        return
    raw_callback = payload.get("callback")
    callback = raw_callback if isinstance(raw_callback, dict) else {}
    callback_id = str(callback.get("callback_id") or "").strip()
    if not callback_id:
        return
    try:
        await MaxBotSender().answer_callback(callback_id=callback_id)
    except Exception:  # validator: allow-wide-except - provider acknowledgement is best effort only
        log.warning(
            "Official MAX owner callback acknowledgement failed",
            exc_info=True,
        )


async def max_webhook(request: web.Request) -> web.Response:
    """Add finite extraction retries around the canonical ClientPlatform MAX entry."""

    payload = _payload_from_body(await request.text())
    if payload is None or not base_ingress._max_secret_ok(request, payload):
        return await base_ingress.max_webhook(request)

    update_type = str(
        payload.get("update_type")
        or payload.get("type")
        or payload.get("event_type")
        or payload.get("event")
        or ""
    ).strip()
    if update_type not in base_ingress._MAX_PROCESSABLE_UPDATE_TYPES:
        return await base_ingress.max_webhook(request)

    extracted = extract_max_message(payload)
    if extracted is None:
        event_key = max_event_key(payload)
        result = await _record_extraction_failure(
            platform="max",
            event_key=event_key,
            payload=payload,
            reason=f"extraction_failed:update_type={update_type or 'unknown'}",
        )
        if result.retryable:
            return web.json_response(
                {"ok": False, "error": "retry", "attempts": result.attempts, "dead_lettered": False},
                status=503,
            )
        return web.json_response(
            {"ok": True, "attempts": result.attempts, "dead_lettered": result.dead_lettered}
        )

    entry_text = base_ingress._entry_start_text(str(extracted.get("text") or ""))
    command = parse_clientplatform_entry_command(entry_text, event_type=update_type)
    if command is None:
        entry_text = "start"

    await _ack_global_max_owner_callback(payload)
    event_key = max_event_key(payload)
    try:
        processed = await asyncio.to_thread(
            _process_clientplatform_entry_and_persist,
            platform="max",
            event_key=event_key,
            event_type=update_type,
            payload=payload,
            extracted=extracted,
            text=entry_text,
        )
    except Exception:  # validator: allow-wide-except
        log.exception("MAX ClientPlatform entry processing failed")
        return web.json_response({"ok": False, "error": "retry"}, status=503)
    if not processed:
        log.info("MAX ClientPlatform entry duplicate skipped")
    return web.json_response({"ok": True})


__all__ = ["extraction_max_attempts", "max_webhook", "vk_webhook"]
