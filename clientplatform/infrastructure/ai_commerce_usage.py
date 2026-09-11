from __future__ import annotations

from typing import Any, Mapping

from clientplatform.ai_commerce.domain import UsageSnapshot
from clientplatform.domain.tenancy import normalize_uuid


STAFF_SEAT_SKU = "staff_seat"
ACTIVE_CUSTOMER_SKU = "active_customer"


class CanonicalSubscriptionUsageProvider:
    """Read canonical usage without creating a private AI-commerce ledger."""

    def __init__(self, conn: Any, *, allowances: Mapping[str, int]) -> None:
        self._conn = conn
        self._allowances = {str(key): int(value) for key, value in allowances.items()}

    def _count(self, *, business_id: str, sku: str) -> int:
        if sku == STAFF_SEAT_SKU:
            sql = "SELECT COUNT(*) FROM business_members WHERE business_id=? AND status='active'"
        elif sku == ACTIVE_CUSTOMER_SKU:
            sql = "SELECT COUNT(*) FROM customers WHERE business_id=? AND status='active'"
        else:
            raise ValueError(f"unsupported canonical commerce sku: {sku}")
        row = self._conn.execute(sql, (business_id,)).fetchone()
        if row is None:
            return 0
        if hasattr(row, "keys"):
            return int(row[0])
        return int(row[0])

    def get_usage(self, *, business_id: str, sku: str) -> UsageSnapshot:
        normalized_business = normalize_uuid(business_id, field_name="business_id")
        normalized_sku = str(sku or "").strip()
        if normalized_sku not in self._allowances:
            raise ValueError(f"allowance is not configured for sku: {normalized_sku}")
        return UsageSnapshot(
            used=self._count(business_id=normalized_business, sku=normalized_sku),
            allowance=self._allowances[normalized_sku],
        )


__all__ = [
    "ACTIVE_CUSTOMER_SKU",
    "CanonicalSubscriptionUsageProvider",
    "STAFF_SEAT_SKU",
]
