from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class OfferingProcessError(ValueError):
    """The rebuildable process attached to an offering is invalid."""


class OfferingProcessNotFound(OfferingProcessError):
    """No process row exists for the canonical offering."""


class OfferingProcessState(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    PURGED = "purged"


@dataclass(frozen=True, slots=True)
class BusinessOfferingProcess:
    """Rebuildable mechanics keyed by the canonical BusinessOffering id.

    The offering remains the source of truth for the service name and business
    history. This object deliberately stores no duplicate title, customer,
    payment, booking or outcome facts.
    """

    offering_id: str
    business_id: str
    state: OfferingProcessState
    revision: int
    mechanics_json: str
    ai_profile_json: str
    created_by_member_id: str
    created_at: str
    updated_at: str
    frozen_at: str | None = None
    purge_after: str | None = None
    purged_at: str | None = None

    @property
    def mechanics(self) -> dict[str, object]:
        return decode_process_mapping(self.mechanics_json)

    @property
    def ai_profile(self) -> dict[str, object]:
        return decode_process_mapping(self.ai_profile_json)



def decode_process_mapping(value: str) -> dict[str, object]:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OfferingProcessError("offering process JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise OfferingProcessError("offering process JSON must be an object")
    return dict(payload)


def encode_process_mapping(
    value: Mapping[str, object] | None,
    *,
    field_name: str,
) -> str:
    payload = dict(value or {})
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise OfferingProcessError(f"{field_name} must be JSON-serializable") from exc
    if len(encoded.encode("utf-8")) > 100_000:
        raise OfferingProcessError(f"{field_name} is too large")
    return encoded


def default_ai_profile() -> dict[str, object]:
    """Provider-neutral AI capabilities; the offering title is resolved live."""

    return {
        "schema_version": 1,
        "ad_copy": {"enabled": True},
        "followup_copy": {"enabled": True},
        "image_generation": {"enabled": True},
    }


def default_process_mechanics() -> dict[str, object]:
    return {
        "schema_version": 1,
        "customer_intake": {"enabled": True},
        "delivery_or_booking": {"enabled": True},
        "promotion": {"enabled": True},
        "sales_followup": {"enabled": True},
    }


__all__ = [
    "BusinessOfferingProcess",
    "OfferingProcessError",
    "OfferingProcessNotFound",
    "OfferingProcessState",
    "decode_process_mapping",
    "default_ai_profile",
    "default_process_mechanics",
    "encode_process_mapping",
]
