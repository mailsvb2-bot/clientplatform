from __future__ import annotations

from typing import Any

from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.sales_ui_repository import SalesUiRepository
from services.db import get_db_ro


def list_sales_work(
    *,
    actor: TenantContext,
    limit: int = 12,
) -> list[dict[str, Any]]:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).list_open_work(actor=actor, limit=limit)


def get_sales_work_item(
    *,
    actor: TenantContext,
    lead_id: str,
) -> dict[str, Any] | None:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).get_work_item(actor=actor, lead_id=lead_id)


def list_recent_closed_sales_work(
    *,
    actor: TenantContext,
    limit: int = 12,
) -> list[dict[str, Any]]:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).list_recent_closed(actor=actor, limit=limit)


def count_sales_handoff_work(*, actor: TenantContext) -> int:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).count_handoff_work(actor=actor)


def list_sales_handoff_work(
    *,
    actor: TenantContext,
    limit: int = 12,
) -> list[dict[str, Any]]:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).list_handoff_work(actor=actor, limit=limit)


def list_commercial_ladders(*, actor: TenantContext) -> list[dict[str, Any]]:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).list_ladders(actor=actor)


def list_commercial_ladder_steps(
    *,
    actor: TenantContext,
    ladder_id: str,
) -> list[dict[str, Any]]:
    with get_db_ro() as conn:
        return SalesUiRepository(conn).list_ladder_steps(
            actor=actor,
            ladder_id=ladder_id,
        )
