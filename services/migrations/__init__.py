from __future__ import annotations

import sqlite3
from services.db import schema as db_schema
from services.schema_core import ensure_prod_tables
from services.migrations.price_rub_migration_v1 import apply as _apply_price
from services.migrations.scheduled_jobs_to_jobs_v1 import apply as _apply_sched
from services.migrations.jobs_job_key_unique_v2 import apply as _apply_jobs_job_key_unique_v2
from services.migrations.events_decision_tracking_v1 import apply as _apply_events
from services.migrations.payments_decision_attribution_v1 import apply as _apply_pay_decision
from services.migrations.user_channel_routing_v1 import apply as _apply_channel_routing
from services.migrations.user_channel_bridge_and_audio_progress_v1 import apply as _apply_channel_bridge_audio
from services.migrations.account_identity_v1 import apply as _apply_account_identity_v1
from services.migrations.user_audio_access_tokens_v1 import apply as _apply_audio_access_tokens
from services.migrations.user_privacy_export_tokens_v1 import apply as _apply_privacy_export_tokens
from services.migrations.user_audio_progress_state_v2 import apply as _apply_audio_progress_state_v2
from services.migrations.user_messenger_runtime_v3 import apply as _apply_messenger_runtime_v3
from services.migrations.messenger_delivery_outbox_v1 import apply as _apply_messenger_delivery_outbox_v1
from services.migrations.messenger_delivery_reply_progress_v2 import apply as _apply_messenger_delivery_reply_progress_v2
from services.migrations.user_audio_timeline_v4 import apply as _apply_audio_timeline_v4
from services.migrations.messenger_media_assets_v5 import apply as _apply_messenger_media_assets_v5
from services.migrations.messenger_media_assets_v6 import apply as _apply_messenger_media_assets_v6
from services.migrations.messenger_media_assets_mtime_double_v7 import apply as _apply_messenger_media_assets_mtime_double_v7
from services.migrations.user_delivery_preferences_v6 import apply as _apply_delivery_preferences_v6
from services.migrations.admin_ad_links_v1 import apply as _apply_admin_ad_links_v1
from services.migrations.growth_conversion_outbox_v1 import apply as _apply_growth_conversion_outbox_v1
from services.migrations.growth_conversion_bridge_state_v2 import apply as _apply_growth_conversion_bridge_state_v2
from services.migrations.growth_apply_gateway_v3 import apply as _apply_growth_apply_gateway_v3
from services.migrations.growth_apply_review_confirmations_v4 import apply as _apply_growth_apply_review_confirmations_v4
from services.migrations.sales_desk_v5 import apply as _apply_sales_desk_v5
from services.migrations.sales_desk_revenue_v6 import apply as _apply_sales_desk_revenue_v6
from services.migrations.practice_token_economy_v1 import apply as _apply_practice_token_economy_v1
from services.migrations.practice_token_audit_v2 import apply as _apply_practice_token_audit_v2
from services.migrations.practice_token_lots_v4 import apply as _apply_practice_token_lots_v4
from services.migrations.practice_reward_grants_v1 import apply as _apply_practice_reward_grants_v1
from services.migrations.practice_journey_consistency_v3 import apply as _apply_practice_journey_consistency_v3
from services.migrations.premium_entitlements_v1 import apply as _apply_premium_entitlements_v1
from services.migrations.gift_claims_v1 import apply as _apply_gift_claims_v1
from services.migrations.gift_claims_recipient_hint_v2 import apply as _apply_gift_claims_recipient_hint_v2
from services.migrations.telegram_stars_refunds_v1 import apply as _apply_telegram_stars_refunds_v1
from services.migrations.yookassa_refunds_v1 import apply as _apply_yookassa_refunds_v1
from services.migrations.payment_reconciliation_retry_v1 import apply as _apply_payment_reconciliation_retry_v1
from services.migrations.postgres_identity_bigint_v1 import apply as _apply_postgres_identity_bigint_v1
from services.migrations.clientplatform_managed_bot_provider_v1 import (
    apply as _apply_clientplatform_managed_bot_provider_v1,
)
from services.migrations.clientplatform_direct_global_ownership_v1 import (
    apply as _apply_clientplatform_direct_global_ownership_v1,
)
from services.migrations.clientplatform_provider_dispatch_sales_followup_v1 import (
    apply as _apply_clientplatform_provider_dispatch_sales_followup_v1,
)
from services.migrations.clientplatform_provider_dispatch_interactions_v1 import (
    apply as _apply_clientplatform_provider_dispatch_interactions_v1,
)
from services.migrations.clientplatform_messenger_setup_telegram_v1 import (
    apply as _apply_clientplatform_messenger_setup_telegram_v1,
)
from services.migrations.clientplatform_business_payment_outcomes_v1 import (
    apply as _apply_clientplatform_business_payment_outcomes_v1,
)
from services.migrations.clientplatform_promotion_channel_max_v1 import (
    apply as _apply_clientplatform_promotion_channel_max_v1,
)
from services.migrations.clientplatform_email_outbound_v1 import (
    apply as _apply_clientplatform_email_outbound_v1,
)


def apply_all_migrations(conn: sqlite3.Connection) -> None:
    """Apply repo migrations safely even on a fresh/empty SQLite file."""

    db_schema.create_or_update_tables(conn)
    ensure_prod_tables(conn)
    _apply_sched(conn)
    _apply_jobs_job_key_unique_v2(conn)
    _apply_events(conn)
    _apply_pay_decision(conn)
    _apply_channel_routing(conn)
    _apply_channel_bridge_audio(conn)
    _apply_account_identity_v1(conn)
    _apply_audio_access_tokens(conn)
    _apply_privacy_export_tokens(conn)
    _apply_audio_progress_state_v2(conn)
    _apply_messenger_runtime_v3(conn)
    _apply_messenger_delivery_outbox_v1(conn)
    _apply_messenger_delivery_reply_progress_v2(conn)
    _apply_audio_timeline_v4(conn)
    _apply_messenger_media_assets_v5(conn)
    _apply_messenger_media_assets_v6(conn)
    _apply_messenger_media_assets_mtime_double_v7(conn)
    _apply_delivery_preferences_v6(conn)
    _apply_admin_ad_links_v1(conn)
    _apply_growth_conversion_outbox_v1(conn)
    _apply_growth_conversion_bridge_state_v2(conn)
    _apply_growth_apply_gateway_v3(conn)
    _apply_growth_apply_review_confirmations_v4(conn)
    _apply_sales_desk_v5(conn)
    _apply_sales_desk_revenue_v6(conn)
    _apply_practice_token_economy_v1(conn)
    _apply_practice_token_audit_v2(conn)
    _apply_practice_token_lots_v4(conn)
    _apply_practice_reward_grants_v1(conn)
    _apply_practice_journey_consistency_v3(conn)
    _apply_premium_entitlements_v1(conn)
    _apply_gift_claims_v1(conn)
    _apply_gift_claims_recipient_hint_v2(conn)
    _apply_telegram_stars_refunds_v1(conn)
    _apply_yookassa_refunds_v1(conn)
    _apply_payment_reconciliation_retry_v1(conn)
    _apply_postgres_identity_bigint_v1(conn)
    _apply_clientplatform_managed_bot_provider_v1(conn)
    _apply_clientplatform_direct_global_ownership_v1(conn)
    _apply_clientplatform_provider_dispatch_sales_followup_v1(conn)
    _apply_clientplatform_provider_dispatch_interactions_v1(conn)
    _apply_clientplatform_messenger_setup_telegram_v1(conn)
    _apply_clientplatform_business_payment_outcomes_v1(conn)
    _apply_clientplatform_promotion_channel_max_v1(conn)
    _apply_clientplatform_email_outbound_v1(conn)
    _apply_price(conn)
