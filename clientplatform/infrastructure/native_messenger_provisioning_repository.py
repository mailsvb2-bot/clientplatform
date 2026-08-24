from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.connections import (
    ConnectionPlatform,
    normalize_external_account_id,
)
from clientplatform.domain.tenancy import TenantContext
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository


_PROVISIONING_PLATFORMS = frozenset({ConnectionPlatform.VK, ConnectionPlatform.MAX})
_DEFAULT_LEASE_SECONDS = 1800
_MIN_LEASE_SECONDS = 60
_MAX_LEASE_SECONDS = 3600


class NativeMessengerProvisioningBusy(RuntimeError):
    """Another process owns provider setup for the same VK/MAX account."""


class NativeMessengerProvisioningLeaseLost(RuntimeError):
    """A stale provider setup attempt tried to mutate newer canonical state."""


@dataclass(frozen=True, slots=True)
class NativeMessengerProvisioningLease:
    business_id: str
    platform: ConnectionPlatform
    external_account_id: str
    lease_token: str
    acquired_at: str
    expires_at: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _platform(value: ConnectionPlatform | str) -> ConnectionPlatform:
    platform = (
        value
        if isinstance(value, ConnectionPlatform)
        else ConnectionPlatform(str(value or "").strip().lower())
    )
    if platform not in _PROVISIONING_PLATFORMS:
        raise ValueError("native messenger provisioning supports only VK or MAX")
    return platform


class NativeMessengerProvisioningRepository:
    """A bounded durable and fenced lease around provider webhook mutation.

    The lease is committed before provider I/O. A crashed process cannot block
    setup forever, while the exact lease token prevents an older attempt from
    disabling or releasing state owned by a newer retry.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def acquire(
        self,
        *,
        actor: TenantContext,
        platform: ConnectionPlatform | str,
        external_account_id: str,
        now: datetime | None = None,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> NativeMessengerProvisioningLease:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        selected_platform = _platform(platform)
        account_id = normalize_external_account_id(external_account_id)
        duration = max(
            _MIN_LEASE_SECONDS,
            min(int(lease_seconds), _MAX_LEASE_SECONDS),
        )
        clock = (now or _utc_now()).astimezone(timezone.utc).replace(microsecond=0)
        acquired_at = _iso(clock)
        expires_at = _iso(clock + timedelta(seconds=duration))
        lease_token = str(uuid4())
        cursor = self._conn.execute(
            """
            INSERT INTO native_messenger_provisioning_leases(
                business_id,platform,external_account_id,lease_token,
                acquired_at,expires_at
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(business_id,platform,external_account_id) DO UPDATE SET
                lease_token=excluded.lease_token,
                acquired_at=excluded.acquired_at,
                expires_at=excluded.expires_at
            WHERE native_messenger_provisioning_leases.expires_at<=excluded.acquired_at
            """,
            (
                current.business_id,
                selected_platform.value,
                account_id,
                lease_token,
                acquired_at,
                expires_at,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise NativeMessengerProvisioningBusy(
                "native messenger provisioning is already in progress"
            )
        return NativeMessengerProvisioningLease(
            business_id=current.business_id,
            platform=selected_platform,
            external_account_id=account_id,
            lease_token=lease_token,
            acquired_at=acquired_at,
            expires_at=expires_at,
        )

    def assert_owned(self, lease: NativeMessengerProvisioningLease) -> None:
        row = self._conn.execute(
            """
            SELECT lease_token
            FROM native_messenger_provisioning_leases
            WHERE business_id=? AND platform=? AND external_account_id=?
              AND lease_token=?
            LIMIT 1
            """,
            (
                lease.business_id,
                lease.platform.value,
                lease.external_account_id,
                lease.lease_token,
            ),
        ).fetchone()
        if row is None:
            raise NativeMessengerProvisioningLeaseLost(
                "native messenger provisioning lease was lost"
            )

    def release(self, lease: NativeMessengerProvisioningLease) -> None:
        cursor = self._conn.execute(
            """
            DELETE FROM native_messenger_provisioning_leases
            WHERE business_id=? AND platform=? AND external_account_id=?
              AND lease_token=?
            """,
            (
                lease.business_id,
                lease.platform.value,
                lease.external_account_id,
                lease.lease_token,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise NativeMessengerProvisioningLeaseLost(
                "native messenger provisioning lease was lost"
            )

    def release_if_owned(self, lease: NativeMessengerProvisioningLease) -> bool:
        cursor = self._conn.execute(
            """
            DELETE FROM native_messenger_provisioning_leases
            WHERE business_id=? AND platform=? AND external_account_id=?
              AND lease_token=?
            """,
            (
                lease.business_id,
                lease.platform.value,
                lease.external_account_id,
                lease.lease_token,
            ),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1


__all__ = [
    "NativeMessengerProvisioningBusy",
    "NativeMessengerProvisioningLease",
    "NativeMessengerProvisioningLeaseLost",
    "NativeMessengerProvisioningRepository",
]
