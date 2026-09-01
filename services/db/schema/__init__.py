from __future__ import annotations

import sqlite3

from . import clientplatform_activity
from . import clientplatform_ad_connections
from . import clientplatform_ad_managed_campaigns
from . import clientplatform_ad_publication_assets
from . import clientplatform_creative_experiments
from . import clientplatform_creative_growth
from . import clientplatform_ad_spend_operations
from . import clientplatform_admin_ops
from . import clientplatform_automation_policy
from . import clientplatform_bookings
from . import clientplatform_outcomes
from . import clientplatform_bot_gateway
from . import clientplatform_bot_provisioning
from . import clientplatform_connections
from . import clientplatform_messenger_channels
from . import clientplatform_provider_dispatch
from . import clientplatform_offer_ladders
from . import clientplatform_customers
from . import clientplatform_external_products
from . import clientplatform_partners
from . import clientplatform_program_media
from . import clientplatform_programs
from . import clientplatform_promotions
from . import clientplatform_attribution
from . import clientplatform_revenue_attribution
from . import clientplatform_sales
from . import clientplatform_sales_ai
from . import clientplatform_tenancy
from . import shared_runtime

# Execution order matters: shared operational primitives first, followed by
# isolated tenant/customer/activity/program/connection/admin boundaries.
# Historical product tables are deliberately not recreated by fresh bootstrap.
PARTS = [
    shared_runtime,
    clientplatform_tenancy,
    clientplatform_customers,
    clientplatform_activity,
    clientplatform_bookings,
    clientplatform_outcomes,
    clientplatform_promotions,
    clientplatform_attribution,
    clientplatform_revenue_attribution,
    clientplatform_partners,
    clientplatform_external_products,
    clientplatform_sales,
    clientplatform_sales_ai,
    clientplatform_offer_ladders,
    clientplatform_ad_connections,
    clientplatform_ad_managed_campaigns,
    clientplatform_ad_publication_assets,
    clientplatform_creative_experiments,
    clientplatform_creative_growth,
    clientplatform_ad_spend_operations,
    clientplatform_programs,
    clientplatform_program_media,
    clientplatform_connections,
    clientplatform_messenger_channels,
    clientplatform_provider_dispatch,
    clientplatform_bot_gateway,
    clientplatform_bot_provisioning,
    clientplatform_admin_ops,
    clientplatform_automation_policy,
]


def create_or_update_tables(c: sqlite3.Connection) -> None:
    """Create tables and add missing columns (idempotent)."""
    for part in PARTS:
        part.ensure(c)
