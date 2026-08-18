from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


CockpitActionKind = Literal["none", "sales_work", "sales_handoff"]


@dataclass(frozen=True, slots=True)
class GrowthCockpitMetric:
    """One explainable owner-facing number.

    ``source`` names the canonical fact/read-model that supplied the value and
    ``meaning`` explains what the number means.  Cockpit presentation layers
    must not manufacture numbers outside this contract.
    """

    key: str
    label: str
    value: int
    source: str
    meaning: str
    unit: str = "count"

    def __post_init__(self) -> None:
        key = str(self.key or "").strip()
        label = str(self.label or "").strip()
        source = str(self.source or "").strip()
        meaning = str(self.meaning or "").strip()
        unit = str(self.unit or "").strip()
        if not key or not label or not source or not meaning or not unit:
            raise ValueError("growth cockpit metric metadata must not be empty")
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("growth cockpit metric value must be an integer")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "meaning", meaning)
        object.__setattr__(self, "unit", unit)


@dataclass(frozen=True, slots=True)
class GrowthCockpitMoney:
    currency: str
    amount_minor: int
    source: str
    meaning: str

    def __post_init__(self) -> None:
        currency = str(self.currency or "").strip().upper()
        source = str(self.source or "").strip()
        meaning = str(self.meaning or "").strip()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise ValueError("growth cockpit currency must be a three-letter ISO code")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise TypeError("growth cockpit money must use integer minor units")
        if not source or not meaning:
            raise ValueError("growth cockpit money metadata must not be empty")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "meaning", meaning)


@dataclass(frozen=True, slots=True)
class GrowthCockpitAction:
    kind: CockpitActionKind
    title: str
    detail: str

    def __post_init__(self) -> None:
        if self.kind not in {"none", "sales_work", "sales_handoff"}:
            raise ValueError("unsupported growth cockpit action kind")
        title = str(self.title or "").strip()
        detail = str(self.detail or "").strip()
        if not title or not detail:
            raise ValueError("growth cockpit action copy must not be empty")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "detail", detail)


@dataclass(frozen=True, slots=True)
class GrowthCockpitSnapshot:
    """Read-only owner view composed from canonical ClientPlatform facts."""

    business_id: str
    timezone: str
    period_days: int
    generated_at: datetime
    metrics: tuple[GrowthCockpitMetric, ...]
    today_revenue: tuple[GrowthCockpitMoney, ...]
    period_revenue: tuple[GrowthCockpitMoney, ...]
    what_worked: str
    requires_decision: GrowthCockpitAction
    next_action: GrowthCockpitAction
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.period_days not in {7, 30}:
            raise ValueError("growth cockpit period must be 7 or 30 days")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("growth cockpit generated_at must be timezone-aware")
        if not str(self.business_id or "").strip() or not str(self.timezone or "").strip():
            raise ValueError("growth cockpit business and timezone are required")
        if not str(self.what_worked or "").strip():
            raise ValueError("growth cockpit what_worked must not be empty")
        keys = [item.key for item in self.metrics]
        if len(keys) != len(set(keys)):
            raise ValueError("growth cockpit metric keys must be unique")
        object.__setattr__(self, "limitations", tuple(dict.fromkeys(self.limitations)))

    def metric(self, key: str) -> GrowthCockpitMetric:
        for item in self.metrics:
            if item.key == key:
                return item
        raise KeyError(key)


__all__ = [
    "GrowthCockpitAction",
    "GrowthCockpitMetric",
    "GrowthCockpitMoney",
    "GrowthCockpitSnapshot",
]
