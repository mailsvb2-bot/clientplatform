from __future__ import annotations

import sqlite3

from . import clientplatform_activity
from . import clientplatform_ad_connections
from . import clientplatform_ad_publication_assets
from . import clientplatform_ad_spend_operations
from . import clientplatform_admin_ops
from . import clientplatform_bookings
from . import clientplatform_bot_gateway
from . import clientplatform_bot_provisioning
from . import clientplatform_connections
from . import clientplatform_provider_dispatch
from . import clientplatform_offer_ladders
from . import clientplatform_customers
from . import clientplatform_partners
from . import clientplatform_program_media
from . import clientplatform_programs
from . import clientplatform_promotions
from . import clientplatform_sales
from . import clientplatform_tenancy
from . import analytics
from . import funnel
from . import gifts
from . import jobs
from . import payments
from . import plans
from . import settings
from . import users

# Execution order matters: legacy users first, then all isolated ClientPlatform
# tenant/customer/activity/program/connection/admin boundaries, then legacy tables.
# Additive ClientPlatform schemas never mutate imported Metrotherapy tables.
PARTS = [
    users,
    clientplatform_tenancy,
    clientplatform_customers,
    clientplatform_activity,
    clientplatform_bookings,
    clientplatform_promotions,
    clientplatform_partners,
    clientplatform_sales,
    clientplatform_offer_ladders,
    clientplatform_ad_connections,
    clientplatform_ad_publication_assets,
    clientplatform_ad_spend_operations,
    clientplatform_programs,
    clientplatform_program_media,
    clientplatform_connections,
    clientplatform_provider_dispatch,
    clientplatform_bot_gateway,
    clientplatform_bot_provisioning,
    clientplatform_admin_ops,
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
    for part in PARTS:
        part.ensure(c)