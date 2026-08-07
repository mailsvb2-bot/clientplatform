from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from clientplatform.domain.bot_provisioning import (
    BotProvisioningInvariantViolation,
    BotProvisioningNotFound,
    BotProvisioningProvider,
    ManagedBotProvisioningRequest,
    VerifiedTelegramBot,
)
from clientplatform.domain.tenancy import TenantContext, normalize_user_id
from clientplatform.infrastructure.bot_provisioning_repository import (
    BotProvisioningRepository,
)
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository


_ACTIVE_STATUSES = ("awaiting_secret", "ready", "verifying", "failed")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class PendingManagedBotOnboarding:
    actor: TenantContext
    request: ManagedBotProvisioningRequest


class ManagedBotOnboardingRepository:
    """Correlate Telegram managed-bot updates to a durable tenant request."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._tenancy = TenancyRepository(conn)
        self._provisioning = BotProvisioningRepository(conn)

    def create(
        self,
        *,
        actor: TenantContext,
        idempotency_key: str,
        suggested_username: str | None = None,
        display_name: str | None = None,
    ) -> ManagedBotProvisioningRequest:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        pending = self._rows_for_user(current.user_id)
        if pending:
            business_ids = {str(row["business_id"]) for row in pending}
            if business_ids == {current.business_id} and len(pending) == 1:
                return self._provisioning.get(
                    actor=current,
                    request_id=str(pending[0]["id"]),
                )
            raise BotProvisioningInvariantViolation(
                "finish the current managed bot setup before starting another"
            )
        return self._provisioning.create_request(
            actor=current,
            provider=BotProvisioningProvider.TELEGRAM_MANAGED,
            idempotency_key=idempotency_key,
            requested_username=suggested_username,
            display_name=display_name,
        )

    def pending_for_user(self, *, user_id: int) -> PendingManagedBotOnboarding:
        normalized_user_id = normalize_user_id(user_id)
        rows = self._rows_for_user(normalized_user_id)
        if not rows:
            raise BotProvisioningNotFound(
                "managed bot creation request was not found"
            )
        if len(rows) != 1:
            raise BotProvisioningInvariantViolation(
                "managed bot creation request is ambiguous"
            )
        business_id = str(rows[0]["business_id"])
        actor = self._tenancy.resolve_context(
            user_id=normalized_user_id,
            business_id=business_id,
        )
        actor.assert_can_manage_business()
        request = self._provisioning.get(
            actor=actor,
            request_id=str(rows[0]["id"]),
        )
        if request.provider != BotProvisioningProvider.TELEGRAM_MANAGED:
            raise BotProvisioningInvariantViolation(
                "managed bot creation provider changed unexpectedly"
            )
        return PendingManagedBotOnboarding(actor=actor, request=request)

    def record_created_bot(
        self,
        *,
        user_id: int,
        external_bot_id: str,
        username: str,
        display_name: str | None = None,
        now: str | None = None,
    ) -> PendingManagedBotOnboarding:
        """Persist child identity before any token retrieval or vault operation."""

        pending = self.pending_for_user(user_id=user_id)
        identity = VerifiedTelegramBot(
            external_bot_id=external_bot_id,
            username=username,
            display_name=display_name,
        )
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            f"""
            UPDATE managed_bot_provisioning_requests
            SET external_bot_id=?, verified_username=?,
                display_name=COALESCE(?, display_name), updated_at=?
            WHERE id=? AND business_id=? AND provider=?
              AND status IN ({','.join('?' for _ in _ACTIVE_STATUSES)})
              AND (external_bot_id IS NULL OR external_bot_id=?)
              AND (verified_username IS NULL OR verified_username=?)
            """,
            (
                identity.external_bot_id,
                identity.username,
                identity.display_name,
                timestamp,
                pending.request.id,
                pending.actor.business_id,
                BotProvisioningProvider.TELEGRAM_MANAGED.value,
                *_ACTIVE_STATUSES,
                identity.external_bot_id,
                identity.username,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            current = self._provisioning.get(
                actor=pending.actor,
                request_id=pending.request.id,
            )
            if (
                current.external_bot_id != identity.external_bot_id
                or current.verified_username != identity.username
            ):
                raise BotProvisioningInvariantViolation(
                    "managed bot creation event conflicts with the active request"
                )
        request = self._provisioning.get(
            actor=pending.actor,
            request_id=pending.request.id,
        )
        return PendingManagedBotOnboarding(actor=pending.actor, request=request)

    def _rows_for_user(self, user_id: int) -> list[Any]:
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        return self._conn.execute(
            f"""
            SELECT request.id, request.business_id
            FROM managed_bot_provisioning_requests AS request
            JOIN business_members AS member
              ON member.id=request.created_by_member_id
             AND member.business_id=request.business_id
            WHERE member.user_id=?
              AND member.status='active'
              AND request.provider=?
              AND request.status IN ({placeholders})
            ORDER BY request.created_at DESC, request.id DESC
            LIMIT 2
            """,
            (
                normalize_user_id(user_id),
                BotProvisioningProvider.TELEGRAM_MANAGED.value,
                *_ACTIVE_STATUSES,
            ),
        ).fetchall()


__all__ = [
    "ManagedBotOnboardingRepository",
    "PendingManagedBotOnboarding",
]
