from __future__ import annotations

from clientplatform.domain.sales_metrics import SalesFunnelSnapshot
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.sales_metrics_repository import SalesMetricsRepository
from services.db import get_db_ro


def get_sales_funnel_snapshot(*, actor: TenantContext) -> SalesFunnelSnapshot:
    with get_db_ro() as conn:
        return SalesMetricsRepository(conn).snapshot(actor=actor)
