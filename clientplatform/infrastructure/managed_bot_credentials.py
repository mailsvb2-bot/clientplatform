from __future__ import annotations

import base64
import os
import re
import stat
import subprocess  # nosec B404
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.safe_tenancy_repository import TenancyRepository


class ManagedBotCredentialError(RuntimeError):
    """Managed Telegram bot credential material could not be handled safely."""


class ManagedBotCredentialVault(Protocol):
    def seal(self, plaintext: str) -> str: ...

    def open(self, ciphertext: str) -> str: ...


class InMemoryManagedBotCredentialVault:
    """Hermetic test vault. Production composition must use age encryption."""

    _PREFIX = "memory-managed-bot-v1:"

    def seal(self, plaintext: str) -> str:
        value = str(plaintext)
        if not value:
            raise ManagedBotCredentialError("managed bot credential must not be empty")
        return self._PREFIX + base64.urlsafe_b64encode(value.encode("utf-8")).decode(
            "ascii"
        )

    def open(self, ciphertext: str) -> str:
        raw = str(ciphertext or "")
        if not raw.startswith(self._PREFIX):
            raise ManagedBotCredentialError("managed bot credential format is invalid")
        try:
            value = base64.urlsafe_b64decode(
                raw.removeprefix(self._PREFIX)
            ).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ManagedBotCredentialError(
                "managed bot credential ciphertext is invalid"
            ) from exc
        if not value:
            raise ManagedBotCredentialError("managed bot credential is empty")
        return value


class AgeManagedBotCredentialVault:
    """Seal managed-bot tokens with a separately provisioned age identity."""

    _PREFIX = "age-managed-bot-v1:"

    def __init__(self, identity_path: str | Path | None = None) -> None:
        if identity_path is not None:
            configured: str | Path = identity_path
        else:
            configured = (
                os.getenv("CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_IDENTITY_FILE")
                or "/run/secrets/clientplatform-managed-bot/identity.txt"
            )
        self._identity_path = Path(configured)

    @property
    def identity_path(self) -> Path:
        return self._identity_path

    def validate_identity(self) -> None:
        """Verify that the configured age identity is safe and usable."""

        self._ensure_identity()

    def seal(self, plaintext: str) -> str:
        value = str(plaintext)
        if not value:
            raise ManagedBotCredentialError("managed bot credential must not be empty")
        recipient = self._recipient()
        # The executable and all switches are fixed; only the validated age
        # recipient is data, and shell execution is never used.
        completed = subprocess.run(  # nosec B603, B607
            ["age", "--encrypt", "--recipient", recipient],
            input=value.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout:
            raise ManagedBotCredentialError("managed bot credential encryption failed")
        return self._PREFIX + base64.urlsafe_b64encode(completed.stdout).decode("ascii")

    def open(self, ciphertext: str) -> str:
        raw = str(ciphertext or "")
        if not raw.startswith(self._PREFIX):
            raise ManagedBotCredentialError("managed bot credential format is invalid")
        self._ensure_identity()
        try:
            payload = base64.urlsafe_b64decode(raw.removeprefix(self._PREFIX))
        except ValueError as exc:
            raise ManagedBotCredentialError(
                "managed bot credential ciphertext is invalid"
            ) from exc
        # The executable and switches are fixed. The identity path is an
        # operator-owned file whose type, mode and owner are checked below.
        completed = subprocess.run(  # nosec B603, B607
            ["age", "--decrypt", "--identity", str(self._identity_path)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            raise ManagedBotCredentialError("managed bot credential decryption failed")
        try:
            value = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManagedBotCredentialError(
                "managed bot credential plaintext encoding is invalid"
            ) from exc
        if not value:
            raise ManagedBotCredentialError("managed bot credential is empty")
        return value

    def _recipient(self) -> str:
        self._ensure_identity()
        # Fixed age-keygen command; shell execution is never used.
        completed = subprocess.run(  # nosec B603, B607
            ["age-keygen", "-y", str(self._identity_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        recipient = completed.stdout.strip()
        if completed.returncode != 0 or not recipient.startswith("age1"):
            raise ManagedBotCredentialError(
                "managed bot credential recipient derivation failed"
            )
        return recipient

    def _ensure_identity(self) -> None:
        try:
            identity_stat = self._identity_path.lstat()
        except FileNotFoundError:
            identity_stat = None
        except OSError as exc:
            raise ManagedBotCredentialError(
                "managed bot credential identity cannot be inspected"
            ) from exc

        if identity_stat is not None:
            self._assert_private_identity(identity_stat)
            return

        if _deployed_environment() or not _allow_identity_generation():
            raise ManagedBotCredentialError(
                "managed bot credential identity must be provisioned before startup"
            )
        self._identity_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._identity_path.parent, 0o700)
        self._assert_private_directory()
        temporary = self._identity_path.with_suffix(".tmp")
        temporary.unlink(missing_ok=True)
        # Fixed local key-generation command; shell execution is never used.
        completed = subprocess.run(  # nosec B603, B607
            ["age-keygen", "-o", str(temporary)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            temporary.unlink(missing_ok=True)
            raise ManagedBotCredentialError(
                "managed bot credential identity generation failed"
            )
        try:
            generated_stat = temporary.lstat()
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ManagedBotCredentialError(
                "managed bot credential identity generation failed"
            ) from exc
        if stat.S_ISLNK(generated_stat.st_mode) or not stat.S_ISREG(
            generated_stat.st_mode
        ):
            temporary.unlink(missing_ok=True)
            raise ManagedBotCredentialError(
                "managed bot credential identity generation was unsafe"
            )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._identity_path)
        self._assert_private_identity(self._identity_path.lstat())

    def _assert_private_directory(self) -> None:
        try:
            directory_stat = self._identity_path.parent.lstat()
        except OSError as exc:
            raise ManagedBotCredentialError(
                "managed bot credential identity directory cannot be inspected"
            ) from exc
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
            directory_stat.st_mode
        ):
            raise ManagedBotCredentialError(
                "managed bot credential identity directory is unsafe"
            )
        if directory_stat.st_mode & 0o777 != 0o700:
            raise ManagedBotCredentialError(
                "managed bot credential identity directory permissions must be 0700"
            )
        if hasattr(os, "geteuid") and directory_stat.st_uid != os.geteuid():
            raise ManagedBotCredentialError(
                "managed bot credential identity directory owner is invalid"
            )

    def _assert_private_identity(self, identity_stat: os.stat_result) -> None:
        self._assert_private_directory()
        if stat.S_ISLNK(identity_stat.st_mode) or not stat.S_ISREG(identity_stat.st_mode):
            raise ManagedBotCredentialError(
                "managed bot credential identity must be a regular file"
            )
        if identity_stat.st_size <= 0:
            raise ManagedBotCredentialError(
                "managed bot credential identity must not be empty"
            )
        if identity_stat.st_mode & 0o777 != 0o600:
            raise ManagedBotCredentialError(
                "managed bot credential identity permissions must be 0600"
            )
        if hasattr(os, "geteuid") and identity_stat.st_uid != os.geteuid():
            raise ManagedBotCredentialError(
                "managed bot credential identity owner is invalid"
            )


_REFERENCE_RE = re.compile(
    r"vault://managed-bot/([0-9a-fA-F-]{36})/([0-9a-fA-F-]{36})"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _external_bot_id(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized.isdigit() or int(normalized) <= 0:
        raise ValueError("managed bot id must be a positive Telegram user id")
    return normalized


def _parse_reference(reference: str) -> tuple[str, str]:
    match = _REFERENCE_RE.fullmatch(str(reference or "").strip())
    if match is None:
        raise ManagedBotCredentialError("managed bot credential reference is invalid")
    return (
        normalize_uuid(match.group(1), field_name="managed_bot_credential_business_id"),
        normalize_uuid(match.group(2), field_name="managed_bot_credential_id"),
    )


class ManagedBotCredentialStore:
    """Persist only encrypted managed-bot tokens and expose opaque references."""

    def __init__(
        self,
        conn: Any,
        *,
        vault: ManagedBotCredentialVault,
    ) -> None:
        self._conn = conn
        self._vault = vault
        self._tenancy = TenancyRepository(conn)

    def put(
        self,
        *,
        actor: TenantContext,
        external_bot_id: str,
        token: str,
        now: str | None = None,
    ) -> str:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        bot_id = _external_bot_id(external_bot_id)
        ciphertext = self._vault.seal(str(token))
        timestamp = str(now or _utc_now())
        credential_id = str(uuid4())
        self._conn.execute(
            """
            INSERT INTO managed_bot_credentials(
                id, business_id, external_bot_id, ciphertext, status,
                created_at, updated_at, revoked_at
            ) VALUES(?, ?, ?, ?, 'active', ?, ?, NULL)
            ON CONFLICT(business_id, external_bot_id) DO UPDATE SET
                ciphertext=excluded.ciphertext,
                status='active',
                updated_at=excluded.updated_at,
                revoked_at=NULL
            """,
            (
                credential_id,
                current.business_id,
                bot_id,
                ciphertext,
                timestamp,
                timestamp,
            ),
        )
        row = self._conn.execute(
            """
            SELECT id FROM managed_bot_credentials
            WHERE business_id=? AND external_bot_id=? AND status='active'
            LIMIT 1
            """,
            (current.business_id, bot_id),
        ).fetchone()
        if row is None:
            raise ManagedBotCredentialError("managed bot credential was not stored")
        stored_id = str(row["id"] if hasattr(row, "keys") else row[0])
        return f"vault://managed-bot/{current.business_id}/{stored_id}"

    def resolve(self, reference: str) -> str:
        business_id, credential_id = _parse_reference(reference)
        row = self._conn.execute(
            """
            SELECT ciphertext FROM managed_bot_credentials
            WHERE id=? AND business_id=? AND status='active'
            LIMIT 1
            """,
            (credential_id, business_id),
        ).fetchone()
        if row is None:
            raise ManagedBotCredentialError("managed bot credential is unavailable")
        ciphertext = str(row["ciphertext"] if hasattr(row, "keys") else row[0])
        return self._vault.open(ciphertext)

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
            UPDATE managed_bot_credentials
            SET ciphertext='revoked', status='revoked', revoked_at=?, updated_at=?
            WHERE id=? AND business_id=? AND status='active'
            """,
            (timestamp, timestamp, credential_id, current.business_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) == 1


def _deployed_environment() -> bool:
    return (os.getenv("APP_ENV") or "dev").strip().lower() in {
        "prod",
        "production",
        "stage",
        "staging",
    }


def _allow_identity_generation() -> bool:
    return (
        os.getenv("CLIENTPLATFORM_MANAGED_BOT_CREDENTIAL_ALLOW_GENERATE") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "AgeManagedBotCredentialVault",
    "InMemoryManagedBotCredentialVault",
    "ManagedBotCredentialError",
    "ManagedBotCredentialStore",
    "ManagedBotCredentialVault",
]
