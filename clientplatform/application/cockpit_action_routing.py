from __future__ import annotations

import re
from dataclasses import dataclass

from clientplatform.application.control_callbacks import token_uuid, uuid_token

_PREFIX = "cpo_c_"
_TOKEN_RE = r"[A-Za-z0-9_-]{22}"
_PAYLOAD_RE = re.compile(
    rf"^{_PREFIX}(?P<business>{_TOKEN_RE})_(?P<kind>[hwl])(?:_(?P<target>{_TOKEN_RE}))?$"
)


@dataclass(frozen=True, slots=True)
class CockpitActionStartRoute:
    business_id: str
    kind: str
    lead_id: str | None = None


def _strict_uuid_from_token(value: str) -> str:
    if re.fullmatch(_TOKEN_RE, str(value or "")) is None:
        raise ValueError("invalid cockpit action token")
    decoded = token_uuid(value)
    if uuid_token(decoded) != value:
        raise ValueError("non-canonical cockpit action token")
    return decoded


def build_cockpit_action_start_payload(*, business_id: str, action_key: str) -> str:
    business_token = uuid_token(business_id)
    key = str(action_key or "").strip()
    if key == "sales_handoff":
        payload = f"{_PREFIX}{business_token}_h"
    elif key.startswith("sales_plan:"):
        # Validate the canonical plan identifier even though the Telegram route
        # opens the existing sales-work surface rather than duplicating plan UI.
        uuid_token(key.split(":", 1)[1])
        payload = f"{_PREFIX}{business_token}_w"
    elif key.startswith("sales_lead:"):
        lead_token = uuid_token(key.split(":", 1)[1])
        payload = f"{_PREFIX}{business_token}_l_{lead_token}"
    else:
        raise ValueError("cockpit customer action has no supported Telegram route")
    if len(payload) > 64:
        raise ValueError("cockpit action payload exceeds Telegram start limit")
    return payload


def parse_cockpit_action_start_payload(payload: str | None) -> CockpitActionStartRoute | None:
    raw = str(payload or "").strip()
    if not raw.startswith(_PREFIX):
        return None
    match = _PAYLOAD_RE.fullmatch(raw)
    if match is None:
        raise ValueError("invalid cockpit action start payload")
    business_id = _strict_uuid_from_token(match.group("business"))
    kind = match.group("kind")
    target = match.group("target")
    if kind == "l":
        if target is None:
            raise ValueError("cockpit lead route requires a lead token")
        lead_id = _strict_uuid_from_token(target)
    else:
        if target is not None:
            raise ValueError("cockpit action route has an unexpected target")
        lead_id = None
    return CockpitActionStartRoute(
        business_id=business_id,
        kind=kind,
        lead_id=lead_id,
    )


__all__ = [
    "CockpitActionStartRoute",
    "build_cockpit_action_start_payload",
    "parse_cockpit_action_start_payload",
]
