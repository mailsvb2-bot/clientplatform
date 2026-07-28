from __future__ import annotations

import asyncio
from dataclasses import dataclass

from a1.domain.connections import ClaimedDispatch, DispatchStatus
from a1.infrastructure import DispatchOutboxRepository
from a1.transport.base import AdapterRegistry, CredentialProvider
from services.db import get_db


@dataclass(frozen=True, slots=True)
class DispatchBatchResult:
    claimed: int
    sent: int
    retried: int
    dead: int


def _sanitize_error(exc: BaseException, *, credential: str = "") -> str:
    message = str(exc or exc.__class__.__name__).strip()
    secret = str(credential or "")
    if secret:
        message = message.replace(secret, "[redacted]")
    if not message:
        message = exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"[:1000]


def _effective_max_attempts(exc: BaseException, configured: int) -> int:
    """Terminal transport/media failures must not consume the retry budget."""

    retryable = getattr(exc, "retryable", True)
    return max(1, int(configured)) if retryable is not False else 1


def _release_claims(
    items: list[ClaimedDispatch],
    *,
    reason: str,
) -> None:
    for item in items:
        try:
            with get_db() as conn:
                DispatchOutboxRepository(conn).release_lease(
                    item,
                    reason=reason,
                )
        except Exception:
            # The lease can already be stale or completed. Cancellation must not
            # be masked by a best-effort cleanup failure.
            continue


async def run_dispatch_batch(
    *,
    credential_provider: CredentialProvider,
    adapters: AdapterRegistry,
    limit: int = 10,
    max_attempts: int = 8,
    lock_ttl_seconds: int = 900,
) -> DispatchBatchResult:
    """Claim, send and settle a bounded batch.

    Database leases are committed before any credential lookup or network I/O.
    Each settlement uses a new short transaction, so a slow provider never
    holds an open database transaction.
    """

    with get_db() as conn:
        claimed = DispatchOutboxRepository(conn).claim_due(
            limit=limit,
            lock_ttl_seconds=lock_ttl_seconds,
        )

    sent = 0
    retried = 0
    dead = 0

    for index, item in enumerate(claimed):
        credential = ""
        try:
            credential = str(
                await asyncio.to_thread(
                    credential_provider.resolve,
                    item.credential_reference,
                )
                or ""
            ).strip()
            if not credential:
                raise ValueError("credential provider returned an empty secret")

            adapter = adapters.get(item.dispatch.platform)
            provider_message_id = await adapter.send(item, credential)
            with get_db() as conn:
                DispatchOutboxRepository(conn).mark_sent(
                    item,
                    provider_message_id=provider_message_id,
                )
            sent += 1
        except asyncio.CancelledError:
            _release_claims(
                claimed[index:],
                reason="worker_cancelled",
            )
            raise
        except Exception as exc:
            error = _sanitize_error(exc, credential=credential)
            with get_db() as conn:
                updated = DispatchOutboxRepository(conn).reschedule(
                    item,
                    error=error,
                    max_attempts=_effective_max_attempts(exc, max_attempts),
                )
            if updated.status == DispatchStatus.DEAD:
                dead += 1
            else:
                retried += 1

    return DispatchBatchResult(
        claimed=len(claimed),
        sent=sent,
        retried=retried,
        dead=dead,
    )
