from __future__ import annotations

import sqlite3

from . import a1_tenancy, analytics, funnel, gifts, jobs, payments, plans, settings, users

# Execution order matters: legacy users first, then the new A1 tenant boundary,
# then dependent legacy tables. The additive tenancy schema does not mutate the
# imported Metrotherapy tables.
PARTS = [
    users,
    a1_tenancy,
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
