from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def normalise_messenger_text(text: str, *, allow_plain_score: bool = False) -> str:
    """Normalize only channel-neutral ClientPlatform entry aliases.

    ``allow_plain_score`` remains as a compatibility keyword for callers from
    older deployments, but ClientPlatform control ingress never interprets bare
    numbers as a product-specific state machine.
    """

    del allow_plain_score
    raw = str(text or "").strip()
    compact = " ".join(raw.casefold().replace("ё", "е").split())
    aliases = {
        "/start": "start",
        "start": "start",
        "старт": "start",
        "начать": "start",
        "menu": "start",
        "/menu": "start",
        "меню": "start",
        "главное меню": "start",
        "⬅️ назад": "start",
        "назад": "start",
        "⬅️ меню": "start",
        "menu:main": "start",
        "back": "start",
    }
    return aliases.get(compact, raw)


def safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def stable_payload_key(platform: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "ignore")
    return f"{platform}:sha256:" + hashlib.sha256(encoded).hexdigest()


def _payload_text(raw: Any, *, prefer_command: bool = False) -> str:
    if raw in (None, "", b""):
        return ""
    payload: Any = raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            return ""
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return value
        if not isinstance(payload, (dict, list)):
            return value
    if isinstance(payload, dict):
        command_keys = ("command", "cmd", "action", "value", "data", "payload", "callback", "button", "body")
        text_keys = ("text", "label")
        keys = command_keys + text_keys if prefer_command else command_keys[:4] + text_keys + command_keys[4:]
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = _payload_text(value, prefer_command=prefer_command)
                if nested:
                    return nested
            if isinstance(value, list):
                for item in value:
                    nested = _payload_text(item, prefer_command=prefer_command)
                    if nested:
                        return nested
    if isinstance(payload, list):
        for item in payload:
            nested = _payload_text(item, prefer_command=prefer_command)
            if nested:
                return nested
    return ""


def text_from_vk_payload(raw: Any) -> str:
    return _payload_text(raw, prefer_command=True)


def text_from_max_payload(raw: Any) -> str:
    return _payload_text(raw, prefer_command=True)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def vk_raw_message(
    payload: Mapping[str, Any],
) -> tuple[str, str | None, str | None] | None:
    obj = _mapping(payload.get("object"))
    message = _mapping(obj.get("message") or obj)
    external = (
        message.get("from_id")
        or message.get("user_id")
        or obj.get("from_id")
        or obj.get("user_id")
    )
    subject = str(external or "").strip()
    if not subject:
        return None
    payload_text = text_from_vk_payload(
        message.get("payload") or obj.get("payload") or payload.get("payload")
    )
    text = str(
        payload_text or message.get("text") or obj.get("text") or ""
    ).strip()
    return subject, text, None


def max_raw_message(
    payload: Mapping[str, Any],
) -> tuple[str, str | None, str | None] | None:
    message = _mapping(payload.get("message"))
    body = _mapping(message.get("body"))
    callback = _mapping(
        payload.get("callback") or payload.get("button") or payload.get("payload")
    )
    sender = _mapping(
        message.get("sender")
        or payload.get("sender")
        or payload.get("user")
        or callback.get("sender")
        or callback.get("user")
    )
    external = (
        sender.get("user_id")
        or sender.get("id")
        or callback.get("user_id")
        or payload.get("user_id")
        or body.get("user_id")
    )
    subject = str(external or "").strip()
    if not subject:
        return None
    callback_text = text_from_max_payload(callback)
    payload_text = (
        callback_text
        or text_from_max_payload(body.get("payload"))
        or text_from_max_payload(message.get("payload"))
        or text_from_max_payload(payload.get("payload"))
    )
    text = str(
        payload_text
        or message.get("text")
        or body.get("text")
        or payload.get("text")
        or ""
    ).strip()
    display_name = (
        str(
            sender.get("display_name")
            or sender.get("name")
            or " ".join(
                part
                for part in (
                    str(sender.get("first_name") or "").strip(),
                    str(sender.get("last_name") or "").strip(),
                )
                if part
            )
            or ""
        ).strip()
        or None
    )
    return subject, text, display_name


def _first_int_from_dict(payload: dict[str, Any], *paths: tuple[str, ...]) -> int | None:
    for path in paths:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        result = safe_int(current)
        if result is not None:
            return result
    return None


def vk_event_key(payload: dict[str, Any]) -> str:
    obj = _dict_or_empty(payload.get("object"))
    message = _dict_or_empty(obj.get("message") or obj)
    parts = [
        str(payload.get("event_id") or obj.get("event_id") or ""),
        str(message.get("id") or message.get("conversation_message_id") or ""),
        str(message.get("from_id") or message.get("user_id") or ""),
        str(message.get("date") or ""),
    ]
    key = ":".join(part for part in parts if part)
    return key or stable_payload_key("vk", payload)


def max_event_key(payload: dict[str, Any]) -> str:
    message = _dict_or_empty(payload.get("message"))
    body = _dict_or_empty(message.get("body"))
    callback = _dict_or_empty(payload.get("callback") or payload.get("button") or payload.get("payload"))
    sender = _dict_or_empty(message.get("sender") or payload.get("sender") or payload.get("user") or callback.get("sender"))
    parts = [
        str(
            payload.get("update_id")
            or payload.get("event_id")
            or callback.get("callback_id")
            or payload.get("timestamp")
            or ""
        ),
        str(message.get("message_id") or message.get("id") or body.get("mid") or callback.get("id") or ""),
        str(sender.get("user_id") or sender.get("id") or payload.get("user_id") or payload.get("chat_id") or ""),
        str(message.get("created_at") or payload.get("created_at") or payload.get("timestamp") or ""),
    ]
    key = ":".join(part for part in parts if part)
    return key or stable_payload_key("max", payload)


def extract_vk_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    obj = _dict_or_empty(payload.get("object"))
    message = _dict_or_empty(obj.get("message") or obj)
    from_id = (
        message.get("from_id")
        or message.get("user_id")
        or message.get("peer_id")
        or obj.get("from_id")
        or obj.get("user_id")
    )
    safe_user_id = safe_int(from_id)
    if safe_user_id is None:
        return None

    payload_text = text_from_vk_payload(message.get("payload") or obj.get("payload") or payload.get("payload"))
    ref = str(message.get("ref") or obj.get("ref") or "").strip()
    if ref.casefold().startswith("cpo_"):
        text = f"/start {ref}"
    else:
        text = (payload_text or message.get("text") or obj.get("text") or "").strip()
        text = normalise_messenger_text(text)
    return {
        "user_id": safe_user_id,
        "external_user_id": str(from_id),
        "username": None,
        "display_name": None,
        "first_name": None,
        "text": text or "start",
    }


def extract_max_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    message = _dict_or_empty(payload.get("message"))
    body = _dict_or_empty(message.get("body"))
    callback = _dict_or_empty(payload.get("callback") or payload.get("button") or payload.get("payload"))

    sender = _dict_or_empty(message.get("sender") or payload.get("sender") or payload.get("user") or callback.get("sender"))

    user_id = _first_int_from_dict(
        {"message": message, "sender": sender, "payload": payload, "callback": callback, "body": body},
        ("message", "sender", "user_id"),
        ("message", "sender", "id"),
        ("sender", "user_id"),
        ("sender", "id"),
        ("callback", "sender", "user_id"),
        ("callback", "sender", "id"),
        ("callback", "user", "user_id"),
        ("callback", "user", "id"),
        ("payload", "user", "user_id"),
        ("payload", "user", "id"),
        ("payload", "user_id"),
        ("payload", "chat_id"),
        ("body", "user_id"),
    )
    if user_id is None:
        return None

    text = (message.get("text") or body.get("text") or payload.get("text") or "").strip()
    command_text = (
        text_from_max_payload(callback)
        or text_from_max_payload(body.get("payload"))
        or text_from_max_payload(message.get("payload"))
        or text_from_max_payload(payload.get("payload"))
    )
    text = command_text or text or "start"
    text = normalise_messenger_text(text)
    return {
        "user_id": int(user_id),
        "external_user_id": str(user_id),
        "username": None,
        "display_name": None,
        "first_name": None,
        "text": text or "start",
    }
