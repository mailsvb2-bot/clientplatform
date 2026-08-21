from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from services.messenger.menu_contract import normalize_menu_command


def _format_score(score: int) -> str:
    return str(int(score))


def _score_command_value(value: str) -> str | None:
    raw = str(value or "").strip().casefold().replace("−", "-")
    candidate: str | None = None
    if raw.startswith("score:"):
        candidate = raw.split(":", 1)[1].strip()
    elif raw.startswith("score="):
        candidate = raw.split("=", 1)[1].strip()
    elif raw.startswith("mood:"):
        parts = raw.split(":")
        if len(parts) >= 4 and parts[1] in {"pre", "post"}:
            candidate = parts[-1].strip()
    if candidate is None:
        return None
    if candidate.startswith("+"):
        candidate = candidate[1:]
    try:
        score = int(candidate)
    except ValueError:
        return None
    if -10 <= score <= 10:
        return _format_score(score)
    return None


def _plain_score_value(value: str, *, allow_one_two: bool = False) -> str | None:
    raw = str(value or "").strip().casefold().replace("−", "-")
    if not raw:
        return None

    if raw.startswith("+") and raw[1:].isdigit():
        candidate = raw[1:]
    elif raw.startswith("-") and raw[1:].isdigit():
        candidate = raw
    elif raw == "0" or raw in {"3", "4", "5", "6", "7", "8", "9", "10"}:
        candidate = raw
    elif allow_one_two and raw in {"1", "2"}:
        candidate = raw
    else:
        return None

    try:
        score = int(candidate)
    except ValueError:
        return None

    if -10 <= score <= 10:
        return _format_score(score)
    return None


def normalise_messenger_text(text: str, *, allow_plain_score: bool = False) -> str:
    raw = (text or "").strip()

    score_command = _score_command_value(raw)
    if score_command is not None:
        return score_command

    plain_score = _plain_score_value(raw, allow_one_two=allow_plain_score)
    if plain_score is not None:
        return plain_score

    compact = raw.casefold().replace("ё", "е")
    compact = " ".join(compact.split())

    command = normalize_menu_command(compact)
    if command:
        return "start" if command == "start" else command

    aliases = {
        "/start": "start",
        "start": "start",
        "старт": "start",
        "начать": "start",
        "🌿 начать": "start",
        "menu": "start",
        "/menu": "start",
        "меню": "start",
        "главное меню": "start",
        "⬅️ назад": "start",
        "назад": "start",
        "⬅️ меню": "start",
        "menu:main": "start",
        "back": "start",
        "demo_kind_work": "demo_work",
        "demo_kind_home": "demo_home",
        "sub:menu": "pay",
        "gift:menu": "gift",
        "settings:menu": "settings",
        "settings:state": "progress",
        "share:menu": "share",
        "weather:show": "weather",
        "weather:city": "weather_city",
        "1": "demo_work",
        "1.": "demo_work",
        "1️⃣": "demo_work",
        "1️⃣ утро / дорога": "demo_work",
        "утро / дорога": "demo_work",
        "утро": "demo_work",
        "дорога на работу": "demo_work",
        "🚗 практика на утро / дорогу": "demo_work",
        "практика на утро / дорогу": "demo_work",
        "2": "demo_home",
        "2.": "demo_home",
        "2️⃣": "demo_home",
        "2️⃣ вечер / домой": "demo_home",
        "вечер / домой": "demo_home",
        "вечер": "demo_home",
        "дорога домой": "demo_home",
        "🌙 практика на вечер / домой": "demo_home",
        "практика на вечер / домой": "demo_home",
        "weather_city": "weather_city",
        "🏙 изменить город": "weather_city",
        "изменить город": "weather_city",
        "сменить город": "weather_city",
        "город": "weather_city",
        "🔄 обновить погоду": "weather",
        "обновить погоду": "weather",
        "💳 оплатить": "pay",
        "оплатить": "pay",
        "оплата": "pay",
        "pay": "pay",
        "⚙️ настройки": "settings",
        "settings": "settings",
        "repeat": "repeat",
        "/repeat": "repeat",
        "🔁 повторить": "repeat",
        "повторить": "repeat",
        "повторить аудио": "repeat",
        "слушать снова": "repeat",
        "📊 прогресс": "progress",
        "🧾 история": "history",
        "история": "history",
        "history": "history",
        "timeline": "history",
        "/timeline": "history",
        "🔁 другой мессенджер": "switch",
        "другой мессенджер": "switch",
        "switch": "switch",
        "❓ помощь": "help",
        "помощь": "help",
        "help": "help",
        "/help": "help",
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
        str(payload.get("event_id") or ""),
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
        str(payload.get("update_id") or payload.get("event_id") or payload.get("timestamp") or ""),
        str(message.get("message_id") or message.get("id") or body.get("mid") or callback.get("id") or ""),
        str(sender.get("user_id") or sender.get("id") or payload.get("user_id") or payload.get("chat_id") or ""),
        str(message.get("created_at") or payload.get("created_at") or payload.get("timestamp") or ""),
    ]
    key = ":".join(part for part in parts if part)
    return key or stable_payload_key("max", payload)


def _has_pending_score_context(user_id: int | None) -> bool:
    if user_id is None:
        return False
    try:
        from services.mood_text_flow import find_pending_pre_session_id, find_pending_post_session_id

        return bool(find_pending_pre_session_id(int(user_id)) or find_pending_post_session_id(int(user_id)))
    except RuntimeError:
        return False
    except (ImportError, TypeError, ValueError):
        return False


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
    text = (payload_text or message.get("text") or obj.get("text") or "").strip()
    text = normalise_messenger_text(text, allow_plain_score=_has_pending_score_context(safe_user_id))
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
    text = normalise_messenger_text(text, allow_plain_score=bool(command_text) or _has_pending_score_context(int(user_id)))
    return {
        "user_id": int(user_id),
        "external_user_id": str(user_id),
        "username": None,
        "display_name": None,
        "first_name": None,
        "text": text or "start",
    }
