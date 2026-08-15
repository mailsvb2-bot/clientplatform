from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from clientplatform.domain.bookings import CustomerBusinessLink
from clientplatform.domain.bot_gateway import (
    AdmittedIngressEvent,
    BotGatewayAdmissionRejected,
    ClaimedIngressEvent,
    IngressEvent,
    ManagedBotRoute,
)
from clientplatform.domain.customers import CustomerPlatform
from clientplatform.domain.messenger_channels import CustomerIngressContext
from clientplatform.infrastructure.managed_bot_polling_repository import (
    ManagedBotPollingRepository,
)
from clientplatform.infrastructure.messenger_channel_repository import MessengerChannelRepository
from clientplatform.infrastructure.safe_bot_gateway_repository import BotGatewayRepository
from services.db import get_db, get_db_ro


def resolve_telegram_route(*, external_bot_id: int | str) -> ManagedBotRoute:
    with get_db_ro() as conn:
        return BotGatewayRepository(conn).resolve_telegram_route(
            external_bot_id=external_bot_id
        )


def list_active_telegram_routes(*, limit: int = 10_000) -> list[ManagedBotRoute]:
    with get_db_ro() as conn:
        return ManagedBotPollingRepository(conn).list_active_routes(limit=limit)


def admit_telegram_update(
    *,
    route: ManagedBotRoute,
    provider_update_id: int | str,
    payload: Mapping[str, Any],
    per_minute_limit: int,
    queue_limit: int,
    max_payload_bytes: int,
) -> AdmittedIngressEvent:
    """Admit only while the polled route is still active."""

    with get_db() as conn:
        repository = BotGatewayRepository(conn)
        current = repository.resolve_telegram_route(
            external_bot_id=route.external_bot_id
        )
        if current.managed_bot_id != route.managed_bot_id:
            raise BotGatewayAdmissionRejected(
                "managed Telegram bot polling route changed before admission"
            )
        return repository.admit_telegram_update(
            route=current,
            provider_update_id=provider_update_id,
            payload=payload,
            per_minute_limit=per_minute_limit,
            queue_limit=queue_limit,
            max_payload_bytes=max_payload_bytes,
        )


def claim_due_ingress_events(
    *,
    limit: int,
    lock_ttl_seconds: int,
) -> list[ClaimedIngressEvent]:
    with get_db() as conn:
        return BotGatewayRepository(conn).claim_due(
            limit=limit,
            lock_ttl_seconds=lock_ttl_seconds,
        )


def mark_ingress_event_processed(item: ClaimedIngressEvent) -> IngressEvent:
    with get_db() as conn:
        return BotGatewayRepository(conn).mark_processed(item)


def reschedule_ingress_event(
    item: ClaimedIngressEvent,
    *,
    error_code: str,
    max_attempts: int,
) -> IngressEvent:
    with get_db() as conn:
        return BotGatewayRepository(conn).reschedule(
            item,
            error_code=error_code,
            max_attempts=max_attempts,
        )


def ensure_telegram_customer_link(
    *,
    route: ManagedBotRoute,
    telegram_user_id: int,
    username: str | None,
    display_name: str | None,
    now: datetime | None = None,
) -> CustomerBusinessLink:
    with get_db() as conn:
        return BotGatewayRepository(conn).ensure_telegram_customer_link(
            route=route,
            telegram_user_id=telegram_user_id,
            username=username,
            display_name=display_name,
            now=now,
        )


def consume_telegram_customer_channel_link(
    *,
    route: ManagedBotRoute,
    token: str,
    telegram_user_id: int,
    username: str | None,
    display_name: str | None,
    now: datetime | None = None,
) -> CustomerBusinessLink:
    """Atomically bind this Telegram identity to an existing canonical customer.

    The managed route is re-resolved inside the same transaction before the link
    token is consumed, so disable/revoke cannot race a stale polling work item.
    """

    with get_db() as conn:
        bot_repository = BotGatewayRepository(conn)
        current = bot_repository.resolve_telegram_route(
            external_bot_id=route.external_bot_id
        )
        if current.managed_bot_id != route.managed_bot_id or current.business_id != route.business_id:
            raise BotGatewayAdmissionRejected(
                "managed Telegram route changed before customer link consume"
            )
        identity = MessengerChannelRepository(conn).consume_customer_link(
            context=CustomerIngressContext(
                business_id=current.business_id,
                connection_id=current.connection_id,
                platform=CustomerPlatform.TELEGRAM,
            ),
            token=token,
            external_subject=str(telegram_user_id),
            username=username,
            display_name=display_name,
            now=now,
        )
        business = conn.execute(
            "SELECT name FROM businesses WHERE id=? AND status='active' LIMIT 1",
            (current.business_id,),
        ).fetchone()
        if business is None:
            raise BotGatewayAdmissionRejected("managed Telegram business is not active")
        business_name = str(business["name"] if hasattr(business, "keys") else business[0])
        return CustomerBusinessLink(
            business_id=current.business_id,
            business_name=business_name,
            customer_id=identity.customer_id,
        )


def bot_gateway_health_snapshot() -> dict[str, int]:
    with get_db_ro() as conn:
        return BotGatewayRepository(conn).health_snapshot()
