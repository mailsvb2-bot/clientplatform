from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from clientplatform.runtime.secrets import EnvironmentCredentialProvider, SecretReferenceError
from runtime.messenger_max_sender import MaxBotSender
from runtime.messenger_transport_errors import MessengerTransportError
from services.db import get_db_ro


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MaxWebhookReconcileCandidate:
    route_id: str
    business_id: str
    provider_token_reference: str
    webhook_secret_reference: str


@dataclass(frozen=True, slots=True)
class MaxWebhookReconcileResult:
    scanned: int
    reconciled: int
    failed: int
    next_cursor: str | None


def _row_value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _public_origin(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("messenger public base URL must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("messenger public base URL is invalid")
    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise ValueError("messenger public base URL is invalid") from exc
    if explicit_port is not None or parsed.path not in {"", "/"}:
        raise ValueError("messenger public base URL must be an HTTPS origin")
    return normalized


def _list_active_max_candidates(
    *,
    cursor: str | None,
    limit: int,
) -> tuple[MaxWebhookReconcileCandidate, ...]:
    normalized_limit = min(max(int(limit), 1), 1000)
    normalized_cursor = str(cursor or "").strip()
    with get_db_ro() as conn:
        if normalized_cursor:
            rows = conn.execute(
                """
                SELECT r.id, r.business_id, c.credential_reference,
                       r.webhook_secret_reference
                FROM messenger_ingress_routes r
                JOIN connections c
                  ON c.id=r.connection_id AND c.business_id=r.business_id
                 AND c.platform=r.platform
                JOIN businesses b
                  ON b.id=r.business_id
                WHERE r.platform='max' AND r.status='active'
                  AND c.status='active' AND b.status='active'
                  AND r.id>?
                ORDER BY r.id
                LIMIT ?
                """,
                (normalized_cursor, normalized_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT r.id, r.business_id, c.credential_reference,
                       r.webhook_secret_reference
                FROM messenger_ingress_routes r
                JOIN connections c
                  ON c.id=r.connection_id AND c.business_id=r.business_id
                 AND c.platform=r.platform
                JOIN businesses b
                  ON b.id=r.business_id
                WHERE r.platform='max' AND r.status='active'
                  AND c.status='active' AND b.status='active'
                ORDER BY r.id
                LIMIT ?
                """,
                (normalized_limit,),
            ).fetchall()
    return tuple(
        MaxWebhookReconcileCandidate(
            route_id=str(_row_value(row, "id", 0)),
            business_id=str(_row_value(row, "business_id", 1)),
            provider_token_reference=str(_row_value(row, "credential_reference", 2)),
            webhook_secret_reference=str(
                _row_value(row, "webhook_secret_reference", 3)
            ),
        )
        for row in rows
    )


async def reconcile_max_webhook_batch(
    *,
    public_base_url: str,
    cursor: str | None = None,
    limit: int = 100,
    request_delay_seconds: float = 0.05,
    credential_provider: EnvironmentCredentialProvider | None = None,
    sender_factory: Callable[[str], MaxBotSender] | None = None,
) -> MaxWebhookReconcileResult:
    """Reassert canonical MAX webhooks after provider-side auto-unsubscribe.

    MAX may remove a webhook subscription after a prolonged delivery outage. This
    runtime repair reads only canonical active routes/connections, resolves their
    existing encrypted credentials, and asks the existing provider adapter to
    reconcile the same URL + secret + event set. It creates no second routing or
    credential source of truth.
    """

    base_url = _public_origin(public_base_url)
    normalized_limit = min(max(int(limit), 1), 1000)
    delay = min(max(float(request_delay_seconds), 0.0), 5.0)
    candidates = await asyncio.to_thread(
        _list_active_max_candidates,
        cursor=cursor,
        limit=normalized_limit,
    )
    if not candidates:
        return MaxWebhookReconcileResult(
            scanned=0,
            reconciled=0,
            failed=0,
            next_cursor=None,
        )

    credentials = credential_provider or EnvironmentCredentialProvider()
    build_sender = sender_factory or (lambda token: MaxBotSender(token=token))
    reconciled = 0
    failed = 0
    for candidate in candidates:
        try:
            provider_token = await asyncio.to_thread(
                credentials.resolve,
                candidate.provider_token_reference,
            )
            webhook_secret = await asyncio.to_thread(
                credentials.resolve,
                candidate.webhook_secret_reference,
            )
            sender = build_sender(provider_token)
            await sender.ensure_webhook_subscription(
                url=(
                    f"{base_url}/clientplatform/webhooks/max/"
                    f"{candidate.route_id}"
                ),
                secret=webhook_secret,
            )
            reconciled += 1
        except (
            SecretReferenceError,
            MessengerTransportError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            failed += 1
            log.warning(
                "MAX webhook reconciliation failed",
                extra={
                    "route_id": candidate.route_id,
                    "business_id": candidate.business_id,
                    "error_type": type(exc).__name__,
                },
            )
        if delay:
            await asyncio.sleep(delay)

    next_cursor = candidates[-1].route_id if len(candidates) >= normalized_limit else None
    return MaxWebhookReconcileResult(
        scanned=len(candidates),
        reconciled=reconciled,
        failed=failed,
        next_cursor=next_cursor,
    )


__all__ = [
    "MaxWebhookReconcileResult",
    "reconcile_max_webhook_batch",
]
