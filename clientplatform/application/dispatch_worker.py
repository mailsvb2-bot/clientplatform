from __future__ import annotations

import asyncio
from dataclasses import dataclass

from clientplatform.domain.connections import ClaimedDispatch, DispatchStatus
from clientplatform.infrastructure import DispatchOutboxRepository
from clientplatform.infrastructure.unified_dispatch_outbox import ClaimedProviderDispatch
from clientplatform.transport.base import AdapterRegistry, CredentialProvider
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


def _partner_claim_can_cross_provider_boundary(item: object) -> bool:
    """Atomically honor a last-moment partner contact revocation.

    Claiming commits before network I/O by design. That leaves a deliberate
    boundary where an owner may revoke contact after claim but before a provider
    call starts. Revalidate the lease in a fresh transaction and convert a
    revoked partner lease to ``cancelled`` instead of resolving credentials or
    calling the adapter.
    """

    if not isinstance(item, ClaimedProviderDispatch):
        return True
    with get_db() as conn:
        repository = DispatchOutboxRepository(conn)
        if repository.cancel_revoked_leased_partner_outreach(item):
            return False
        return repository.partner_dispatch_still_authorized(item)


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
    holds an open database transaction. Partner work is revalidated once more
    after claim and before credential resolution/provider I/O so an operator's
    latest contact revocation is honored at the send boundary.
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
            allowed = await asyncio.to_thread(
                _partner_claim_can_cross_provider_boundary,
                item,
            )
            if not allowed:
                continue

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
