from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clientplatform.domain.connections import (
    ConnectionPlatform,
    normalize_external_account_id,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.managed_bot_credentials import (
    AgeManagedBotCredentialVault,
    ManagedBotCredentialError,
    ManagedBotCredentialVault,
)
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository


class ConnectionCredentialError(RuntimeError):
    """Encrypted native-messenger credential material is unavailable or invalid."""


_REFERENCE_RE = re.compile(
    r"vault://connection/([0-9a-fA-F-]{36})/([0-9a-fA-F-]{36})"
)
_PURPOSES = frozenset({"provider_token", "webhook_secret", "confirmation_code"})
_PLATFORMS = frozenset({ConnectionPlatform.VK, ConnectionPlatform.MAX})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _platform(value: ConnectionPlatform | str) -> ConnectionPlatform:
    platform = value if isinstance(value, ConnectionPlatform) else ConnectionPlatform(str(value).strip().lower())
    if platform not in _PLATFORMS:
        raise ValueError("connection credential platform must be VK or MAX")
    return platform


def _purpose(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _PURPOSES:
        raise ValueError("connection credential purpose is invalid")
    return normalized


def _parse_reference(reference: str) -> tuple[str, str]:
    match = _REFERENCE_RE.fullmatch(str(reference or "").strip())
    if match is None:
        raise ConnectionCredentialError("connection credential reference is invalid")
    return (
        normalize_uuid(match.group(1), field_name="connection_credential_business_id"),
        normalize_uuid(match.group(2), field_name="connection_credential_id"),
    )


def connection_credential_reference_business_id(reference: str) -> str | None:
    raw = str(reference or "").strip()
    if not raw.startswith("vault://connection/"):
        return None
    business_id, _credential_id = _parse_reference(raw)
    return business_id


def assert_connection_credential_reference_business(reference: str, business_id: str) -> None:
    reference_business_id = connection_credential_reference_business_id(reference)
    if reference_business_id is None:
        return
    expected = normalize_uuid(business_id, field_name="business_id")
    if reference_business_id != expected:
        raise ConnectionCredentialError(
            "connection credential reference belongs to another business"
        )


class ConnectionCredentialStore:
    """Persist encrypted VK/MAX secrets and expose only business-bound opaque refs."""

    def __init__(
        self,
        conn: Any,
        *,
        vault: ManagedBotCredentialVault | None = None,
    ) -> None:
        self._conn = conn
        self._vault = vault or AgeManagedBotCredentialVault()
        self._tenancy = TenancyRepository(conn)

    def put(
        self,
        *,
        actor: TenantContext,
        platform: ConnectionPlatform | str,
        external_account_id: str,
        purpose: str,
        plaintext: str,
        now: str | None = None,
    ) -> str:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        normalized_platform = _platform(platform)
        normalized_account = normalize_external_account_id(external_account_id)
        normalized_purpose = _purpose(purpose)
        value = str(plaintext or "")
        if not value.strip():
            raise ConnectionCredentialError("connection credential must not be empty")
        try:
            ciphertext = self._vault.seal(value)
        except ManagedBotCredentialError as exc:
            raise ConnectionCredentialError("connection credential encryption failed") from exc
        timestamp = str(now or _utc_now())
        credential_id = str(uuid4())
        self._conn.execute(
            """
            INSERT INTO connection_credentials(
                id, business_id, platform, external_account_id, purpose,
                ciphertext, status, created_at, updated_at, revoked_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)
            ON CONFLICT(business_id, platform, external_account_id, purpose) DO UPDATE SET
                ciphertext=excluded.ciphertext,
                status='active', updated_at=excluded.updated_at, revoked_at=NULL
            """,
            (
                credential_id,
                current.business_id,
                normalized_platform.value,
                normalized_account,
                normalized_purpose,
                ciphertext,
                timestamp,
                timestamp,
            ),
        )
        row = self._conn.execute(
            """
            SELECT id FROM connection_credentials
            WHERE business_id=? AND platform=? AND external_account_id=?
              AND purpose=? AND status='active'
            LIMIT 1
            """,
            (
                current.business_id,
                normalized_platform.value,
                normalized_account,
                normalized_purpose,
            ),
        ).fetchone()
        if row is None:
            raise ConnectionCredentialError("connection credential was not stored")
        stored_id = str(row["id"] if hasattr(row, "keys") else row[0])
        return f"vault://connection/{current.business_id}/{stored_id}"

    def resolve(self, reference: str) -> str:
        business_id, credential_id = _parse_reference(reference)
        row = self._conn.execute(
            """
            SELECT ciphertext FROM connection_credentials
            WHERE id=? AND business_id=? AND status='active'
            LIMIT 1
            """,
            (credential_id, business_id),
        ).fetchone()
        if row is None:
            raise ConnectionCredentialError("connection credential is unavailable")
        ciphertext = str(row["ciphertext"] if hasattr(row, "keys") else row[0])
        try:
            value = self._vault.open(ciphertext)
        except ManagedBotCredentialError as exc:
            raise ConnectionCredentialError("connection credential decryption failed") from exc
        if not value:
            raise ConnectionCredentialError("connection credential is empty")
        return value

    def revoke(
        self,
        *,
        actor: TenantContext,
        reference: str,
        now: str | None = None,
    ) -> bool:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        business_id, credential_id = _parse_reference(reference)
        current.assert_business(business_id)
        timestamp = str(now or _utc_now())
        cursor = self._conn.execute(
            """
            UPDATE connection_credentials
            SET ciphertext='revoked', status='revoked', revoked_at=?, updated_at=?
            WHERE id=? AND business_id=? AND status='active'
            """,
            (timestamp, timestamp, credential_id, current.business_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1


__all__ = [
    "ConnectionCredentialError",
    "ConnectionCredentialStore",
    "assert_connection_credential_reference_business",
    "connection_credential_reference_business_id",
]
