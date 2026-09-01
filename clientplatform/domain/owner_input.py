from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class OwnerInputSession:
    user_id: int
    platform: str
    business_id: str
    action: str
    context: Mapping[str, str]
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))


@dataclass(frozen=True, slots=True)
class OwnerInputResolution:
    action: str
    args: tuple[str, ...]
