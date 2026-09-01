from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from services.events import log_runtime_event

log = logging.getLogger(__name__)

_ACTION_TOKEN_RE = re.compile(r"^[a-z0-9_./-]{1,64}$")


def _safe_meta(**meta: Any) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            safe[key] = value
        else:
            safe[key] = str(value)
    return safe


def _fingerprint(value: str | None) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def classify_messenger_action(value: str | None) -> str:
    """Return a low-cardinality action label without retaining user text or tokens."""

    raw = str(value or "").strip()
    if not raw:
        return "empty"
    lowered = raw.casefold()
    if lowered.startswith(("/start ", "start ", "bridge_", "ref_", "cpo_", "cpa_", "cpj_")):
        return "start_payload"
    if lowered.startswith("/"):
        command = lowered.split(maxsplit=1)[0]
        return command if _ACTION_TOKEN_RE.fullmatch(command) else "command"
    if lowered in {"+1", "+2", "-1", "-2"} or lowered.startswith(("score:", "score=")):
        return "score"
    if ":" in lowered:
        prefix = lowered.split(":", 1)[0]
        if _ACTION_TOKEN_RE.fullmatch(prefix):
            return prefix
    if _ACTION_TOKEN_RE.fullmatch(lowered):
        return lowered
    return "text"


def log_button_rendered(
    *,
    platform: str,
    user_id: int | None,
    surface: str,
    command: str,
    label: str,
) -> None:
    safe_command = classify_messenger_action(command)
    log.info(
        "messenger_button_rendered platform=%s user_id=%s surface=%s action=%s label_len=%s",
        platform,
        user_id,
        surface,
        safe_command,
        len(str(label or "")),
    )
    if user_id is None:
        return
    log_runtime_event(
        int(user_id),
        event_type="messenger_button_rendered",
        source=str(platform or "messenger"),
        payload=_safe_meta(
            surface=surface,
            action=safe_command,
            label_len=len(str(label or "")),
        ),
    )


def log_payload_normalized(
    *,
    platform: str,
    user_id: int | None,
    raw_text: str,
    normalized_text: str,
    event_key: str | None = None,
) -> None:
    raw = str(raw_text or "")
    normalized = str(normalized_text or "")
    action = classify_messenger_action(normalized)
    event_key_hash = _fingerprint(event_key)
    log.info(
        "messenger_payload_normalized platform=%s user_id=%s action=%s raw_len=%s normalized_len=%s changed=%s event_key_hash=%s",
        platform,
        user_id,
        action,
        len(raw),
        len(normalized),
        raw != normalized,
        event_key_hash,
    )
    if user_id is None:
        return
    log_runtime_event(
        int(user_id),
        event_type="messenger_payload_normalized",
        source=str(platform or "messenger"),
        payload=_safe_meta(
            action=action,
            raw_len=len(raw),
            normalized_len=len(normalized),
            changed=raw != normalized,
            event_key_hash=event_key_hash,
        ),
    )


def log_action_completed(
    *,
    platform: str,
    user_id: int,
    action: str,
    replies: int,
    status: str = "ok",
) -> None:
    safe_action = classify_messenger_action(action)
    log.info(
        "messenger_action_completed platform=%s user_id=%s action=%s replies=%s status=%s",
        platform,
        user_id,
        safe_action,
        replies,
        status,
    )
    log_runtime_event(
        int(user_id),
        event_type="messenger_action_completed",
        source=str(platform or "messenger"),
        payload=_safe_meta(action=safe_action, replies=int(replies), status=status),
    )
