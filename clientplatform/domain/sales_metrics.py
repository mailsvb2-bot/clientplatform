from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SalesFunnelCounts:
    discovered: int = 0
    engaged: int = 0
    qualified: int = 0
    checkout: int = 0
    won: int = 0
    lost: int = 0
    open_handoffs: int = 0

    def __post_init__(self) -> None:
        for name in (
            "discovered",
            "engaged",
            "qualified",
            "checkout",
            "won",
            "lost",
            "open_handoffs",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be a non-negative integer")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")

    @staticmethod
    def _percent(numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return round((int(numerator) / int(denominator)) * 100.0, 1)

    @property
    def engagement_percent(self) -> float:
        return self._percent(self.engaged, self.discovered)

    @property
    def qualification_percent(self) -> float:
        return self._percent(self.qualified, self.engaged)

    @property
    def checkout_percent(self) -> float:
        return self._percent(self.checkout, self.qualified)

    @property
    def win_percent(self) -> float:
        return self._percent(self.won, self.discovered)


@dataclass(frozen=True, slots=True)
class SalesFunnelSnapshot:
    total: SalesFunnelCounts
    by_source: dict[str, SalesFunnelCounts]


__all__ = ["SalesFunnelCounts", "SalesFunnelSnapshot"]
