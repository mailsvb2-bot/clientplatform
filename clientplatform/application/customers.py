from __future__ import annotations

from clientplatform.domain.customers import (
    Customer,
    CustomerIdentity,
    CustomerPlatform,
    CustomerRecord,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.customer_repository import CustomerRepository
from services.db import get_db, get_db_ro


def create_customer(
    *,
    actor: TenantContext,
    display_name: str | None = None,
) -> Customer:
    with get_db() as conn:
        return CustomerRepository(conn).create_customer(
            actor=actor,
            display_name=display_name,
        )


def get_customer(
    *,
    actor: TenantContext,
    customer_id: str,
) -> CustomerRecord:
    with get_db_ro() as conn:
        return CustomerRepository(conn).get_customer(
            actor=actor,
            customer_id=customer_id,
        )


def list_customers(
    *,
    actor: TenantContext,
    include_archived: bool = False,
) -> list[Customer]:
    with get_db_ro() as conn:
        return CustomerRepository(conn).list_customers(
            actor=actor,
            include_archived=include_archived,
        )


def list_customers_with_active_identity(
    *,
    actor: TenantContext,
    platform: CustomerPlatform | str,
    limit: int = 100,
) -> list[Customer]:
    with get_db_ro() as conn:
        return CustomerRepository(conn).list_customers_with_active_identity(
            actor=actor,
            platform=platform,
            limit=limit,
        )


def attach_customer_identity(
    *,
    actor: TenantContext,
    customer_id: str,
    platform: CustomerPlatform | str,
    external_subject: str,
    username: str | None = None,
    display_name: str | None = None,
) -> CustomerIdentity:
    with get_db() as conn:
        return CustomerRepository(conn).attach_identity(
            actor=actor,
            customer_id=customer_id,
            platform=platform,
            external_subject=external_subject,
            username=username,
            display_name=display_name,
        )


def find_customer_by_identity(
    *,
    actor: TenantContext,
    platform: CustomerPlatform | str,
    external_subject: str,
) -> CustomerRecord:
    with get_db_ro() as conn:
        return CustomerRepository(conn).find_by_identity(
            actor=actor,
            platform=platform,
            external_subject=external_subject,
        )


def archive_customer(
    *,
    actor: TenantContext,
    customer_id: str,
) -> Customer:
    with get_db() as conn:
        return CustomerRepository(conn).archive_customer(
            actor=actor,
            customer_id=customer_id,
        )
