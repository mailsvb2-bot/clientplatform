from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class VisualScenePlanStatus(StrEnum):
    PLANNING = "planning"
    READY = "ready"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class VisualScenePlanReceipt:
    id: str
    business_id: str
    created_by_member_id: str
    plan_key: str
    request_text: str
    style_json: str
    result_json: str
    status: VisualScenePlanStatus
    created_at: str
    updated_at: str


__all__ = ["VisualScenePlanReceipt", "VisualScenePlanStatus"]
