from __future__ import annotations

from dataclasses import dataclass

from services.accounts.identity import resolve_account_for_identity
from services.store import store
from services.events import log_event
from services.messenger.preferences import record_channel_identity, record_channel_touch, prefer_current_platform
from services.messenger.bridge import consume_bridge_token_and_link
from services.messenger.platforms import normalize_platform


@dataclass(frozen=True)
class StartPayload:
    raw: str
    kind: str
    value: str | None = None


@dataclass(frozen=True)
class EntryActionResult:
    user_id: int
    platform: str
    payload: StartPayload
    linked_via_bridge: bool = False


def parse_start_payload(raw_payload: str | None) -> StartPayload:
    payload = (raw_payload or "").strip()
    if not payload:
        return StartPayload(raw="", kind="plain", value=None)
    if payload.startswith("bridge_"):
        value = payload.replace("bridge_", "", 1).strip()
        return StartPayload(raw=payload, kind="bridge", value=value or None)
    return StartPayload(raw=payload, kind="plain", value=payload)


def _resolve_or_create_entry_account(
    *,
    user_id: int,
    platform: str,
    external_user_id: str | None,
    username: str | None,
    display_name: str | None,
) -> int:
    resolved_account_id = resolve_account_for_identity(
        platform,
        external_user_id,
        proposed_user_id=int(user_id),
        username=username,
        display_name=display_name,
    )
    return int(resolved_account_id if resolved_account_id is not None else int(user_id))


def register_user_entry(
    user_id: int,
    *,
    platform: str,
    external_user_id: str | None,
    username: str | None = None,
    display_name: str | None = None,
    first_name: str | None = None,
    start_payload: str | None = None,
) -> EntryActionResult:
    norm = normalize_platform(platform)
    parsed = parse_start_payload(start_payload)
    canonical_user_id = int(user_id)
    linked_via_bridge = False

    if parsed.kind == "bridge" and parsed.value:
        resolved = consume_bridge_token_and_link(
            parsed.value,
            platform=norm,
            external_user_id=external_user_id,
            username=username,
            display_name=display_name,
        )
        if resolved is not None:
            canonical_user_id = int(resolved.canonical_user_id)
            linked_via_bridge = True
        else:
            canonical_user_id = _resolve_or_create_entry_account(
                user_id=int(user_id),
                platform=norm,
                external_user_id=external_user_id,
                username=username,
                display_name=display_name,
            )
    else:
        canonical_user_id = _resolve_or_create_entry_account(
            user_id=int(user_id),
            platform=norm,
            external_user_id=external_user_id,
            username=username,
            display_name=display_name,
        )

    store.ensure_user(int(canonical_user_id), username, first_name)
    # Backward-compatible mirror: existing delivery/progress services still read
    # user_channel_* tables by canonical user_id while the new account layer is
    # adopted service-by-service.
    record_channel_identity(
        int(canonical_user_id),
        norm,
        external_user_id,
        username=username,
        display_name=display_name,
    )
    record_channel_touch(int(canonical_user_id), norm)

    if linked_via_bridge:
        prefer_current_platform(int(canonical_user_id), norm)
        log_event(int(canonical_user_id), "channel_bridge_linked", {"platform": norm})
    return EntryActionResult(
        user_id=int(canonical_user_id),
        platform=norm,
        payload=parsed,
        linked_via_bridge=linked_via_bridge,
    )
