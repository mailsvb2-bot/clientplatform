from __future__ import annotations

"""Canonical runtime-environment normalization.

Every process accepts the human-friendly ``production`` alias, but internal
runtime decisions operate on one value only: ``prod``. Normalizing once at the
configuration boundary prevents individual modules from drifting between
``APP_ENV == 'prod'`` and ``APP_ENV in {'prod', 'production'}`` checks.
"""

import os

PRODUCTION_ENV = "prod"
_PRODUCTION_ALIASES = frozenset({"prod", "production"})


def normalize_app_env(value: str | None = None, *, default: str = "dev") -> str:
    raw = os.getenv("APP_ENV", default) if value is None else value
    normalized = str(raw or default).strip().lower()
    return PRODUCTION_ENV if normalized in _PRODUCTION_ALIASES else normalized


def is_production_env(value: str | None = None) -> bool:
    return normalize_app_env(value) == PRODUCTION_ENV
