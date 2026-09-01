from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from typing import Any

from aiohttp import web

from config.settings import settings
from runtime.messenger_payloads import (
    extract_max_message,
    extract_vk_message,
    max_event_key,
    vk_event_key,
)
from runtime.messenger_senders import MessengerTransportError, VkBotSender
from services.events import log_event
from services.messenger.clientplatform_entry import (
    handle_clientplatform_entry,
    parse_clientplatform_entry_command,
)
from services.messenger.delivery_outbox import persist_reply_bundle
from services.messenger.observability import log_payload_normalized
from services.messenger.webhook_dedupe import claim_inbound_event, fail_inbound_event

log = logging.getLogger(__name__)

VK_PROCESSABLE_EVENT_TYPES = {"message_new", "message_event"}
_MAX_PROCESSABLE_UPDATE_TYPES = {
    "",
    "message_created",
    "message_callback",
    "bot_started",
    "bot_start",
    "chat_started",
    "conversation_started",
    "button_callback",
    "callback_query",
}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _app_env() -> str:
    return (os.getenv("APP_ENV") or getattr(settings, "APP_ENV", "") or "dev").strip().lower()


def _deployed_env() -> bool:
    return _app_env() in {"prod", "production", "stage", "staging"}


def _allow_insecure_messenger_webhooks() -> bool:
    if _deployed_env():
        return False
    return _env_bool("ALLOW_INSECURE_MESSENGER_WEBHOOKS", False)


def _provided_max_secret(request: web.Request) -> str:
    return str(
        request.headers.get("X-Max-Webhook-Secret")
        or request.headers.get("X-Webhook-Secret")
        or ""
    ).strip()


def _max_secret_ok(request: web.Request, payload: dict[str, Any]) -> bool:
    del payload
    expected = (getattr(settings, "MAX_WEBHOOK_SECRET", "") or "").strip()
    if not expected:
        return _allow_insecure_messenger_webhooks()
    provided = _provided_max_secret(request)
    return bool(provided and hmac.compare_digest(provided, expected))


def _vk_secret_ok(payload: dict[str, Any]) -> bool:
    expected = (getattr(settings, "VK_SECRET", "") or "").strip()
    provided = str(payload.get("secret") or "").strip()
    if not expected:
        return _allow_insecure_messenger_webhooks()
    return bool(provided and hmac.compare_digest(provided, expected))


def _entry_start_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return "start"
    lowered = raw.casefold()
    if lowered.startswith("/start ") or lowered.startswith("start "):
        payload = raw.split(maxsplit=1)[1].strip()
        return f"/start {payload}" if payload else "start"
    if lowered.startswith("cpo_"):
        return f"/start {raw}"
    return raw


def _official_entry_text(text: str, *, event_type: str) -> str:
    del event_type
    # Preserve free-form owner text. The canonical entry handler decides whether
    # it is an active onboarding continuation or a generic request for the menu.
    return _entry_start_text(text)


def _vk_dedupe_key(payload: dict[str, Any]) -> str:
    obj = payload.get("object") or {}
    if isinstance(obj, dict):
        event_id = str(obj.get("event_id") or "").strip()
        user_id = str(obj.get("user_id") or obj.get("peer_id") or "").strip()
        if event_id and user_id:
            return f"{event_id}:{user_id}"
        if event_id:
            return event_id
    return vk_event_key(payload)


def _vk_event_context(payload: dict[str, Any]) -> tuple[str, str, str] | None:
    obj = payload.get("object") or {}
    if not isinstance(obj, dict):
        return None
    event_id = str(obj.get("event_id") or "").strip()
    user_id = str(obj.get("user_id") or "").strip()
    peer_id = str(obj.get("peer_id") or user_id).strip()
    if not event_id or not user_id:
        return None
    return event_id, user_id, peer_id


async def _ack_vk_message_event(payload: dict[str, Any]) -> None:
    if not _env_bool("VK_CALLBACK_SNACKBAR_ENABLED", False):
        return
    context = _vk_event_context(payload)
    if context is None:
        return
    event_id, user_id, peer_id = context
    try:
        await VkBotSender().answer_message_event(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
        )
        log.info("VK message_event acknowledged")
    except MessengerTransportError:
        log.exception("VK message_event acknowledgement failed")


def _process_and_persist(
    *,
    platform: str,
    event_key: str,
    payload: dict[str, Any],
    extracted: dict[str, Any],
    event_type: str,
) -> tuple[bool, int, int]:
    """Persist one official ClientPlatform owner/member interaction atomically."""

    if not claim_inbound_event(platform, event_key, payload):
        return False, 0, 0
    try:
        entry_text = _official_entry_text(
            str(extracted.get("text") or ""),
            event_type=event_type,
        )
        log_payload_normalized(
            platform=platform,
            user_id=extracted["user_id"],
            raw_text=str(extracted.get("text") or ""),
            normalized_text=entry_text,
            event_key=event_key,
        )
        command = parse_clientplatform_entry_command(entry_text, event_type=event_type)
        canonical_user_id, replies = handle_clientplatform_entry(
            extracted["user_id"],
            platform=platform,
            external_user_id=extracted["external_user_id"],
            text=entry_text,
            event_type=event_type,
            username=extracted.get("username"),
            display_name=extracted.get("display_name"),
            first_name=extracted.get("first_name"),
            event_key=event_key,
            fallback_unknown_to_start=True,
        )
        command_action = command.action if command is not None else "owner_text"
        action = f"clientplatform_{command_action}"
        log.info(
            "%s %s processed: canonical_user_id=%s action=%s replies=%s",
            platform.upper(),
            event_type,
            canonical_user_id,
            action,
            len(replies),
        )
        log_event(
            int(canonical_user_id),
            f"{platform}_clientplatform_entry",
            {"action": command_action, "text_len": len(str(extracted.get("text") or ""))},
        )
        persist_reply_bundle(
            platform=platform,
            external_user_id=extracted["external_user_id"],
            canonical_user_id=int(canonical_user_id),
            event_key=event_key,
            replies=list(replies),
            action=action,
        )
        return True, int(canonical_user_id), len(replies)
    except Exception as exc:
        fail_inbound_event(platform, event_key, payload, type(exc).__name__)
        raise


async def vk_webhook(request: web.Request) -> web.Response:
    body = await request.text()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return web.Response(status=400, text="invalid json")
    if not isinstance(payload, dict):
        return web.Response(status=400, text="bad payload")
    if not _vk_secret_ok(payload):
        log.warning("VK webhook rejected: bad or missing secret")
        return web.Response(status=403, text="forbidden")

    event_type = str(payload.get("type") or "").strip()
    if event_type == "confirmation":
        return web.Response(text=(settings.VK_CONFIRMATION_TOKEN or "").strip())
    if event_type not in VK_PROCESSABLE_EVENT_TYPES:
        return web.Response(text="ok")
    if event_type == "message_event":
        await _ack_vk_message_event(payload)

    extracted = extract_vk_message(payload)
    if not extracted:
        return web.Response(text="ok")
    event_key = _vk_dedupe_key(payload)
    try:
        processed, _, _ = await asyncio.to_thread(
            _process_and_persist,
            platform="vk",
            event_key=event_key,
            payload=payload,
            extracted=extracted,
            event_type=event_type,
        )
    except (RuntimeError, OSError, ValueError, TypeError, KeyError):
        log.exception("VK ClientPlatform webhook processing failed")
        return web.Response(status=503, text="retry")
    if not processed:
        log.info("VK ClientPlatform webhook duplicate skipped")
    return web.Response(text="ok")


async def max_webhook(request: web.Request) -> web.Response:
    body = await request.text()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "bad_payload"}, status=400)
    if not _max_secret_ok(request, payload):
        log.warning("MAX webhook rejected: bad or missing secret")
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)

    update_type = str(
        payload.get("update_type")
        or payload.get("type")
        or payload.get("event_type")
        or ""
    ).strip()
    if update_type not in _MAX_PROCESSABLE_UPDATE_TYPES:
        return web.json_response({"ok": True})

    extracted = extract_max_message(payload)
    if not extracted:
        return web.json_response({"ok": True})
    event_key = max_event_key(payload)
    try:
        processed, _, _ = await asyncio.to_thread(
            _process_and_persist,
            platform="max",
            event_key=event_key,
            payload=payload,
            extracted=extracted,
            event_type=update_type,
        )
    except (RuntimeError, OSError, ValueError, TypeError, KeyError):
        log.exception("MAX ClientPlatform webhook processing failed")
        return web.json_response({"ok": False, "error": "retry"}, status=503)
    if not processed:
        log.info("MAX ClientPlatform webhook duplicate skipped: update_type=%r", update_type)
    return web.json_response({"ok": True})


__all__ = [
    "VK_PROCESSABLE_EVENT_TYPES",
    "_MAX_PROCESSABLE_UPDATE_TYPES",
    "_ack_vk_message_event",
    "_entry_start_text",
    "_max_secret_ok",
    "_vk_dedupe_key",
    "_vk_secret_ok",
    "max_webhook",
    "vk_webhook",
]
