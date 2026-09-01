from __future__ import annotations

"""Shared safety primitives for synthetic probes that can mutate live storage."""

import os
import uuid

from services.probe_ledger import (
    SYNTHETIC_USER_ID_MAX,
    SYNTHETIC_USER_ID_MIN,
    assert_synthetic_user_id,
)

PROBE_MUTATION_AUTH_ENV = "CLIENTPLATFORM_PROBE_ALLOW_LIVE_DB_MUTATION"


class ProbeMutationAuthorizationRequired(RuntimeError):
    """Raised before any DB access when a mutating probe was not authorized."""


class ProbeInvariantError(RuntimeError):
    """Raised when a synthetic probe observes a broken production invariant."""


def mutation_authorized(explicit: bool) -> bool:
    return bool(explicit) or (os.getenv(PROBE_MUTATION_AUTH_ENV) or "").strip() == "1"


def require_live_db_mutation(allowed: bool) -> None:
    if not bool(allowed):
        raise ProbeMutationAuthorizationRequired("probe_mutation_authorization_required")


def new_synthetic_user_id() -> int:
    namespace_size = int(SYNTHETIC_USER_ID_MAX) - int(SYNTHETIC_USER_ID_MIN) + 1
    offset = int(uuid.uuid4().hex[:12], 16) % namespace_size
    user_id = int(SYNTHETIC_USER_ID_MAX) - offset
    assert_synthetic_user_id(user_id)
    return user_id


def safe_probe_error_code(exc: BaseException) -> str:
    return f"probe_failure:{type(exc).__name__}"
