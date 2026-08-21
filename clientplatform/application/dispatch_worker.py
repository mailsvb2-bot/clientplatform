from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from typing import Any, Protocol

from clientplatform.domain.connections import (
    ClaimedDispatch,
    ConnectionPlatform,
    DispatchStatus,
)
from clientplatform.domain.customer_interactions import CustomerInteractionMessage
from clientplatform.domain.programs import ContentKind
from clientplatform.infrastructure import DispatchOutboxRepository
from clientplatform.infrastructure.safe_dispatch_outbox import (
    mark_non_replay_safe_dispatch_boundary,
)
from clientplatform.infrastructure.unified_dispatch_outbox import ClaimedProviderDispatch
from clientplatform.transport.base import AdapterRegistry, CredentialProvider
from services.db import get_db


_RUNTIME_LINKS_KEY = "_runtime_link_buttons"
_SETUP_COMMAND_PREFIX = "cpm:setup:"


class InteractionLinkResolver(Protocol):
    def __call__(self, *, command: str, business_id: str) -> str | None: ...


class NativeInteractionLinkResolutionError(RuntimeError):
    """A short-lived native interaction link could not be prepared safely."""

    retryable = False


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


def _effective_max_attempts(
    exc: BaseException,
    configured: int,
    *,
    non_replay_boundary_crossed: bool = False,
) -> int:
    """Terminal or non-replay-safe failures must not consume a retry budget."""

    if non_replay_boundary_crossed:
        return 1
    retryable = getattr(exc, "retryable", True)
    return max(1, int(configured)) if retryable is not False else 1


def _release_claims(
    items: list[Any],
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


def _claims_releasable_after_cancel(
    items: list[Any],
    *,
    current_index: int,
    non_replay_boundary_crossed: bool,
) -> list[Any]:
    """Never requeue the current send after a non-replay provider call began."""

    start = current_index + 1 if non_replay_boundary_crossed else current_index
    return items[start:]


def _provider_claim_can_cross_provider_boundary(item: object) -> bool:
    """Revalidate live authority after claim and immediately before provider I/O."""

    if not isinstance(item, ClaimedProviderDispatch):
        return True
    with get_db() as conn:
        repository = DispatchOutboxRepository(conn)
        if item.dispatch.source_kind == "partner_outreach":
            if repository.cancel_revoked_leased_partner_outreach(item):
                return False
            return repository.partner_dispatch_still_authorized(item)
        if item.dispatch.source_kind == "sales_followup":
            return repository.sales_followup_claim_can_cross_provider_boundary(item)
        if item.dispatch.source_kind in {
            "customer_interaction",
            "member_interaction",
        }:
            return repository.native_interaction_claim_can_cross_provider_boundary(item)
        return True


def _validated_runtime_link(value: object) -> str:
    url = str(value or "").strip()
    if (
        not url.startswith("https://")
        or len(url) > 2048
        or any(ord(char) < 32 or ord(char) == 127 for char in url)
    ):
        raise NativeInteractionLinkResolutionError(
            "native interaction link resolver returned an invalid HTTPS URL"
        )
    return url


def _materialize_native_interaction_links(
    item: object,
    resolver: InteractionLinkResolver | None,
) -> object:
    """Resolve short-lived staff links only in memory before provider I/O.

    The durable outbox keeps only the non-secret ``cpm:setup:<session UUID>``
    command. The bearer URL is inserted into a transient payload copy after
    live-recipient revalidation and before credential lookup/non-replay marking.
    """

    if not isinstance(item, ClaimedProviderDispatch):
        return item
    dispatch = item.dispatch
    if (
        dispatch.source_kind != "member_interaction"
        or dispatch.payload_kind != ContentKind.MIXED
    ):
        return item

    interaction = CustomerInteractionMessage.from_json(dispatch.payload_ref)
    setup_commands = {
        button.command
        for row in interaction.rows
        for button in row
        if button.command.startswith(_SETUP_COMMAND_PREFIX)
    }
    if not setup_commands:
        return item
    if resolver is None:
        raise NativeInteractionLinkResolutionError(
            "native setup link resolver is unavailable"
        )

    links: dict[str, str] = {}
    for command in sorted(setup_commands):
        try:
            resolved = resolver(
                command=command,
                business_id=dispatch.business_id,
            )
        except NativeInteractionLinkResolutionError:
            raise
        except Exception as exc:
            raise NativeInteractionLinkResolutionError(
                "native setup link is expired, revoked or unavailable"
            ) from exc
        if resolved is None:
            raise NativeInteractionLinkResolutionError(
                "native setup command was not resolved"
            )
        links[command] = _validated_runtime_link(resolved)

    try:
        raw_payload = json.loads(dispatch.payload_ref)
    except json.JSONDecodeError as exc:  # pragma: no cover - domain parser already guards this
        raise NativeInteractionLinkResolutionError(
            "native interaction payload is invalid"
        ) from exc
    if not isinstance(raw_payload, dict):  # pragma: no cover - domain parser already guards this
        raise NativeInteractionLinkResolutionError(
            "native interaction payload is invalid"
        )
    raw_payload[_RUNTIME_LINKS_KEY] = links
    runtime_payload = json.dumps(
        raw_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return replace(
        item,
        dispatch=replace(dispatch, payload_ref=runtime_payload),
    )


def _mark_non_replay_boundary(item: object) -> bool:
    if isinstance(item, ClaimedProviderDispatch):
        with get_db() as conn:
            return DispatchOutboxRepository(conn).mark_provider_non_replay_boundary(item)
    if not isinstance(item, ClaimedDispatch):
        return False
    if item.dispatch.platform != ConnectionPlatform.MAX:
        return False
    with get_db() as conn:
        mark_non_replay_safe_dispatch_boundary(conn, item)
    return True


async def run_dispatch_batch(
    *,
    credential_provider: CredentialProvider,
    adapters: AdapterRegistry,
    limit: int = 10,
    max_attempts: int = 8,
    lock_ttl_seconds: int = 900,
    interaction_link_resolver: InteractionLinkResolver | None = None,
) -> DispatchBatchResult:
    """Claim, send and settle a bounded batch.

    Database leases are committed before any credential lookup or network I/O.
    Each settlement uses a new short transaction, so a slow provider never
    holds an open database transaction. Partner, sales follow-up and native
    customer/staff work are revalidated once more after claim and before
    credential resolution/provider I/O so revocation is honored at the send
    boundary.

    Short-lived native staff setup URLs are reconstructed only after that live
    authority check and only in the in-memory claimed item. They never replace
    the digest/session reference stored in the durable outbox.

    Non-idempotent provider calls are durably marked immediately before network
    I/O. Once that boundary is crossed, failure or worker cancellation must never
    return the current work to automatic retry; stale ownership is quarantined by
    the canonical outbox as ambiguous/manual-reconciliation work instead.
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
        non_replay_boundary_crossed = False
        try:
            allowed = await asyncio.to_thread(
                _provider_claim_can_cross_provider_boundary,
                item,
            )
            if not allowed:
                continue

            send_item = await asyncio.to_thread(
                _materialize_native_interaction_links,
                item,
                interaction_link_resolver,
            )
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
            non_replay_boundary_crossed = await asyncio.to_thread(
                _mark_non_replay_boundary,
                item,
            )
            provider_message_id = await adapter.send(send_item, credential)
            with get_db() as conn:
                DispatchOutboxRepository(conn).mark_sent(
                    item,
                    provider_message_id=provider_message_id,
                )
            sent += 1
        except asyncio.CancelledError:
            _release_claims(
                _claims_releasable_after_cancel(
                    claimed,
                    current_index=index,
                    non_replay_boundary_crossed=non_replay_boundary_crossed,
                ),
                reason="worker_cancelled",
            )
            raise
        except Exception as exc:
            error = _sanitize_error(exc, credential=credential)
            with get_db() as conn:
                updated = DispatchOutboxRepository(conn).reschedule(
                    item,
                    error=error,
                    max_attempts=_effective_max_attempts(
                        exc,
                        max_attempts,
                        non_replay_boundary_crossed=non_replay_boundary_crossed,
                    ),
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
