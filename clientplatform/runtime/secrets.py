from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Protocol

from clientplatform.domain.connections import (
    ConnectionInvariantViolation,
    normalize_credential_reference,
)
from clientplatform.infrastructure.connection_credentials import (
    ConnectionCredentialError,
    ConnectionCredentialStore,
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
_CONNECTION_REFERENCE_PREFIX = "vault://connection/"
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,127}")


class EnvironmentCredentialProvider:
    """Resolve approved ClientPlatform credential references.

    The historical class name is retained for API compatibility. Environment
    references remain supported for operator-provisioned secrets, while managed
    Telegram bots may use age-encrypted ``vault://managed-bot/...`` references.
    Raw secret values are never accepted as references or included in errors.
    """

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        allowed_name_prefix: str = "CLIENTPLATFORM_SECRET_",
        managed_bot_vault: ManagedBotCredentialVault | None = None,
        connection_vault: ManagedBotCredentialVault | None = None,
    ) -> None:
        self._environment = environment if environment is not None else os.environ
        self._allowed_name_prefix = str(
            allowed_name_prefix or "CLIENTPLATFORM_SECRET_"
        ).strip()
        self._managed_bot_vault = managed_bot_vault
        self._connection_vault = connection_vault
        if not _ENV_NAME_RE.fullmatch(self._allowed_name_prefix.rstrip("_")):
            raise ValueError("allowed secret environment prefix is invalid")

    def resolve(self, reference: str) -> str:
        try:
            normalized = normalize_credential_reference(reference)
        except (ValueError, ConnectionInvariantViolation):
            raise SecretReferenceError("secret reference is invalid") from None

        if normalized.startswith(_ENV_REFERENCE_PREFIX):
            return self._resolve_environment(normalized)
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
        if normalized.startswith(_CONNECTION_REFERENCE_PREFIX):
            vault = self._connection_vault or AgeManagedBotCredentialVault()
            try:
                with get_db_ro() as conn:
                    return ConnectionCredentialStore(conn, vault=vault).resolve(
                        normalized
                    )
            except ConnectionCredentialError:
                raise SecretReferenceError(
                    "connection credential reference is unavailable"
                ) from None
        raise SecretReferenceError("secret reference provider is not configured")

    def _resolve_environment(self, reference: str) -> str:
        variable_name = reference[len(_ENV_REFERENCE_PREFIX) :]
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


class ClientPlatformCredentialProvider(EnvironmentCredentialProvider):
    """Canonical name for the multi-source ClientPlatform credential resolver."""


__all__ = [
    "ClientPlatformCredentialProvider",
    "CredentialProvider",
    "EnvironmentCredentialProvider",
    "SecretReferenceError",
]
