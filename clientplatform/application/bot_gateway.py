from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from clientplatform.domain.bookings import CustomerBusinessLink
from clientplatform.domain.bot_gateway import (
    AdmittedIngressEvent,
    ClaimedIngressEvent,
    IngressEvent,
    ManagedBotRoute,
)
from clientplatform.infrastructure.safe_bot_gateway_repository import BotGatewayRepository
from services.db import get_db, get_db_ro


def resolve_telegram_route(*, external_bot_id: int | str) -> ManagedBotRoute:
    with get_db_ro() as conn:
        return BotGatewayRepository(conn).resolve_telegram_route(
            external_bot_id=external_bot_id
        )


def admit_telegram_update(
    *,
    route: ManagedBotRoute,
    provider_update_id: int | str,
    payload: Mapping[str, Any],
    per_minute_limit: int,
    queue_limit: int,
    max_payload_bytes: int,
) -> AdmittedIngressEvent:
    with get_db() as conn:
        return BotGatewayRepository(conn).admit_telegram_update(
            route=route,
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


def bot_gateway_health_snapshot() -> dict[str, int]:
    with get_db_ro() as conn:
        return BotGatewayRepository(conn).health_snapshot()
