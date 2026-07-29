from __future__ import annotations

import sqlite3

from . import clientplatform_activity
from . import clientplatform_bookings
from . import clientplatform_bot_gateway
from . import clientplatform_connections
from . import clientplatform_customers
from . import clientplatform_programs
from . import clientplatform_tenancy
from . import analytics
from . import funnel
from . import gifts
from . import jobs
from . import payments
from . import plans
from . import settings
from . import users

# Execution order matters: legacy users first, then clientplatform tenant, customer,
# program-delivery, connection/outbox and managed-bot gateway boundaries, then legacy
# tables. Additive clientplatform schemas do not mutate imported Metrotherapy tables.
PARTS = [
    users,
    clientplatform_tenancy,
    clientplatform_customers,
    clientplatform_activity,
    clientplatform_bookings,
    clientplatform_programs,
    clientplatform_connections,
    clientplatform_bot_gateway,
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
