from __future__ import annotations

import os
import re
from collections.abc import Mapping

from a1.domain.connections import (
    ConnectionInvariantViolation,
    normalize_credential_reference,
)


class SecretReferenceError(RuntimeError):
    """A secret reference cannot be resolved without exposing secret material."""


_ENV_REFERENCE_PREFIX = "secret://env/"
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,127}")


class EnvironmentCredentialProvider:
    """Resolve reviewed environment-backed secret references.

    Only references shaped as ``secret://env/A1_SECRET_*`` are accepted. Raw
    values never appear in exception messages and the environment variable name
    itself must use the dedicated A1 secret namespace.
    """

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        allowed_name_prefix: str = "A1_SECRET_",
    ) -> None:
        self._environment = environment if environment is not None else os.environ
        self._allowed_name_prefix = str(allowed_name_prefix or "A1_SECRET_").strip()
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
            raise SecretReferenceError("secret environment reference is outside A1 namespace")

        value = self._environment.get(variable_name)
        if value is None or not str(value).strip():
            raise SecretReferenceError("secret reference is unavailable")
        return str(value).strip()
