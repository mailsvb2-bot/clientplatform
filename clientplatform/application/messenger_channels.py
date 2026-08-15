from __future__ import annotations

from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.customers import CustomerIdentity, CustomerPlatform
from clientplatform.domain.messenger_channels import IssuedCustomerLink, MessengerIngressRoute
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.messenger_channel_repository import MessengerChannelRepository
from services.db import get_db, get_db_ro


def register_messenger_ingress_route(
    *,
    actor: TenantContext,
    connection_id: str,
    external_route_id: str,
    webhook_secret_reference: str,
) -> MessengerIngressRoute:
    with get_db() as conn:
        return MessengerChannelRepository(conn).register_route(
            actor=actor,
            connection_id=connection_id,
            external_route_id=external_route_id,
            webhook_secret_reference=webhook_secret_reference,
        )


def resolve_messenger_ingress_route(
    *,
    route_id: str,
    expected_platform: ConnectionPlatform | str,
) -> MessengerIngressRoute:
    """Resolve only an active server-owned route; caller must still verify its secret."""
    with get_db_ro() as conn:
        return MessengerChannelRepository(conn).resolve_route(
            route_id=route_id,
            expected_platform=expected_platform,
        )


def ensure_channel_customer(
    *,
    route: MessengerIngressRoute,
    external_subject: str,
    username: str | None = None,
    display_name: str | None = None,
) -> CustomerIdentity:
    """Resolve/create a customer only behind a previously resolved server route."""
    with get_db() as conn:
        current_route = MessengerChannelRepository(conn).resolve_route(
            route_id=route.id,
            expected_platform=route.platform,
        )
        if current_route != route:
            raise ValueError("messenger route changed before customer admission")
        return MessengerChannelRepository(conn).ensure_customer_identity(
            context=current_route.customer_context,
            external_subject=external_subject,
            username=username,
            display_name=display_name,
        )


def issue_customer_channel_link(
    *,
    actor: TenantContext,
    customer_id: str,
    target_platform: CustomerPlatform | str | None = None,
    ttl_seconds: int = 900,
) -> IssuedCustomerLink:
    with get_db() as conn:
        return MessengerChannelRepository(conn).issue_customer_link(
            actor=actor,
            customer_id=customer_id,
            target_platform=target_platform,
            ttl_seconds=ttl_seconds,
        )


def consume_customer_channel_link(
    *,
    route: MessengerIngressRoute,
    token: str,
    external_subject: str,
    username: str | None = None,
    display_name: str | None = None,
) -> CustomerIdentity:
    """Atomically bind a channel after revalidating the active server-owned route."""
    with get_db() as conn:
        current_route = MessengerChannelRepository(conn).resolve_route(
            route_id=route.id,
            expected_platform=route.platform,
        )
        if current_route != route:
            raise ValueError("messenger route changed before customer link consume")
        return MessengerChannelRepository(conn).consume_customer_link(
            context=current_route.customer_context,
            token=token,
            external_subject=external_subject,
            username=username,
            display_name=display_name,
        )
