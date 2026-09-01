from __future__ import annotations

import sqlite3

from services.db import schema as db_schema
from services.schema_core import ensure_prod_tables
from services.migrations.scheduled_jobs_to_jobs_v1 import apply as _apply_scheduled_jobs
from services.migrations.jobs_job_key_unique_v2 import apply as _apply_jobs_unique
from services.migrations.events_decision_tracking_v1 import apply as _apply_events
from services.migrations.user_channel_routing_v1 import apply as _apply_channel_routing
from services.migrations.user_channel_bridge_v1 import apply as _apply_channel_bridge
from services.migrations.account_identity_v1 import apply as _apply_account_identity
from services.migrations.user_privacy_export_tokens_v1 import apply as _apply_privacy_export_tokens
from services.migrations.user_messenger_runtime_v3 import apply as _apply_messenger_runtime
from services.migrations.messenger_delivery_outbox_v1 import apply as _apply_delivery_outbox
from services.migrations.messenger_delivery_reply_progress_v2 import apply as _apply_delivery_progress
from services.migrations.messenger_media_assets_v5 import apply as _apply_media_assets_v5
from services.migrations.messenger_media_assets_v6 import apply as _apply_media_assets_v6
from services.migrations.messenger_media_assets_mtime_double_v7 import apply as _apply_media_assets_v7
from services.migrations.postgres_identity_bigint_v1 import apply as _apply_postgres_identity_bigint
from services.migrations.postgres_account_bigint_v2 import apply as _apply_postgres_account_bigint
from services.migrations.clientplatform_managed_bot_provider_v1 import apply as _apply_managed_bot_provider
from services.migrations.clientplatform_direct_global_ownership_v1 import apply as _apply_direct_global_ownership
from services.migrations.clientplatform_provider_dispatch_sales_followup_v1 import apply as _apply_provider_sales_followup
from services.migrations.clientplatform_provider_dispatch_interactions_v1 import apply as _apply_provider_interactions
from services.migrations.clientplatform_messenger_setup_telegram_v1 import apply as _apply_messenger_setup_telegram
from services.migrations.clientplatform_business_payment_outcomes_v1 import apply as _apply_business_payment_outcomes
from services.migrations.clientplatform_promotion_channel_max_v1 import apply as _apply_promotion_max
from services.migrations.clientplatform_email_outbound_v1 import apply as _apply_email_outbound


def apply_all_migrations(conn: sqlite3.Connection) -> None:
    """Apply only canonical ClientPlatform and shared operational migrations."""

    db_schema.create_or_update_tables(conn)
    ensure_prod_tables(conn)
    _apply_scheduled_jobs(conn)
    _apply_jobs_unique(conn)
    _apply_events(conn)
    _apply_channel_routing(conn)
    _apply_channel_bridge(conn)
    _apply_account_identity(conn)
    _apply_privacy_export_tokens(conn)
    _apply_messenger_runtime(conn)
    _apply_delivery_outbox(conn)
    _apply_delivery_progress(conn)
    _apply_media_assets_v5(conn)
    _apply_media_assets_v6(conn)
    _apply_media_assets_v7(conn)
    _apply_postgres_identity_bigint(conn)
    _apply_postgres_account_bigint(conn)
    _apply_managed_bot_provider(conn)
    _apply_direct_global_ownership(conn)
    _apply_provider_sales_followup(conn)
    _apply_provider_interactions(conn)
    _apply_messenger_setup_telegram(conn)
    _apply_business_payment_outcomes(conn)
    _apply_promotion_max(conn)
    _apply_email_outbound(conn)


__all__ = ["apply_all_migrations"]
