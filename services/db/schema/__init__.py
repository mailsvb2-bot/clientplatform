from __future__ import annotations

import sqlite3

from . import a1_connections
from . import a1_customers
from . import a1_programs
from . import a1_tenancy
from . import analytics
from . import funnel
from . import gifts
from . import jobs
from . import payments
from . import plans
from . import settings
from . import users

# Execution order matters: legacy users first, then A1 tenant, customer,
# program-delivery and connection/outbox boundaries, then legacy tables.
# Additive A1 schemas do not mutate imported Metrotherapy tables.
PARTS = [
    users,
    a1_tenancy,
    a1_customers,
    a1_programs,
    a1_connections,
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
