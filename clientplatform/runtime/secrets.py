from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Protocol

from clientplatform.domain.connections import (
    ConnectionInvariantViolation,
    normalize_credential_reference,
)
from clientplatform.infrastructure.managed_bot_credentials import (
    AgeManagedBotCredentialVault,
    ManagedBotCredentialError,
    ManagedBotCredentialStore,
    ManagedBotCredentialVault,
)
from services.db import get_db_ro


class SecretReferenceError(RuntimeError):
    """A secret reference cannot be resolved without exposing secret material."""


class CredentialProvider(Protocol):
    def resolve(self, reference: str) -> str: ...


_ENV_REFERENCE_PREFIX = "secret://env/"
_MANAGED_BOT_REFERENCE_PREFIX = "vault://managed-bot/"
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,127}")


class EnvironmentCredentialProvider:
    """Resolve reviewed environment-backed secret references.

    Only references shaped as ``secret://env/CLIENTPLATFORM_SECRET_*`` are accepted. Raw
    values never appear in exception messages and the environment variable name
    itself must use the dedicated clientplatform secret namespace.
    """

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        allowed_name_prefix: str = "CLIENTPLATFORM_SECRET_",
    ) -> None:
        self._environment = environment if environment is not None else os.environ
        self._allowed_name_prefix = str(
            allowed_name_prefix or "CLIENTPLATFORM_SECRET_"
        ).strip()
        if not _ENV_NAME_RE.fullmatch(self._allowed_name_prefix.rstrip("_")):
            raise ValueError("allowed secret environment prefix is invalid")

    def resolve(self, reference: str) -> str:
        try:
            normalized = normalize_credential_reference(reference)
        except (ValueError, ConnectionInvariantViolation):
            raise SecretReferenceError("secret reference is invalid") from None
        if not normalized.startswith(_ENV_REFERENCE_PREFIX):
            raise SecretReferenceError("secret reference provider is not configured")

        variable_name = normalized[len(_ENV_REFERENCE_PREFIX) :]
        if not _ENV_NAME_RE.fullmatch(variable_name):
            raise SecretReferenceError("secret environment reference is invalid")
        if not variable_name.startswith(self._allowed_name_prefix):
            raise SecretReferenceError(
                "secret environment reference is outside clientplatform namespace"
            )

        value = self._environment.get(variable_name)
        if value is None or not str(value).strip():
            raise SecretReferenceError("secret reference is unavailable")
        return str(value).strip()


class ClientPlatformCredentialProvider:
    """Resolve both operator-provisioned and encrypted runtime credentials.

    BotFather fallback credentials remain environment-backed. Tokens of Telegram
    Managed Bots are never copied into environment variables; they are stored as
    age-encrypted ciphertext and addressed only by an opaque ``vault://`` reference.
    """

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        managed_bot_vault: ManagedBotCredentialVault | None = None,
    ) -> None:
        self._environment = EnvironmentCredentialProvider(environment)
        self._managed_bot_vault = managed_bot_vault

    def resolve(self, reference: str) -> str:
        try:
            normalized = normalize_credential_reference(reference)
        except (ValueError, ConnectionInvariantViolation):
            raise SecretReferenceError("secret reference is invalid") from None
        if normalized.startswith(_ENV_REFERENCE_PREFIX):
            return self._environment.resolve(normalized)
        if normalized.startswith(_MANAGED_BOT_REFERENCE_PREFIX):
            vault = self._managed_bot_vault or AgeManagedBotCredentialVault()
            try:
                with get_db_ro() as conn:
                    return ManagedBotCredentialStore(conn, vault=vault).resolve(
                        normalized
                    )
            except ManagedBotCredentialError:
                raise SecretReferenceError(
                    "managed bot credential reference is unavailable"
                ) from None
        raise SecretReferenceError("secret reference provider is not configured")


__all__ = [
    "ClientPlatformCredentialProvider",
    "CredentialProvider",
    "EnvironmentCredentialProvider",
    "SecretReferenceError",
]
