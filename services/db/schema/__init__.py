from __future__ import annotations

import sqlite3

from . import (
    a1_customers,
    a1_tenancy,
    analytics,
    funnel,
    gifts,
    jobs,
    payments,
    plans,
    settings,
    users,
)

# Execution order matters: legacy users first, then the A1 tenant boundary and
# its dependent customer model, then dependent legacy tables. Additive A1
# schemas do not mutate imported Metrotherapy tables.
PARTS = [
    users,
    a1_tenancy,
    a1_customers,
    plans,
    payments,
    gifts,
    funnel,
    analytics,
    jobs,
    settings,
]


def create_or_update_tables(c: sqlite3.Connection) -> None:
    """Create tables and add missing columns (idempotent)."""
    for p in PARTS:
        p.ensure(c)
