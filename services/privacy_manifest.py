from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

Disposition = Literal["erase", "retain", "anonymize"]
MANIFEST_VERSION = "2026-09-01.v2-clientplatform-retired-surfaces"

# Global user-owned tables are discovered independently from the currently active
# policies. This keeps privacy validation fail-closed when a historical or new
# table still carries a user/account identifier. Business-scoped tables are
# governed by clientplatform.privacy_manifest instead.
OWNERSHIP_COLUMN_CANDIDATES = frozenset(
    {
        "user_id",
        "account_id",
        "primary_user_id",
        "canonical_user_id",
        "consumed_account_id",
        "buyer_user_id",
        "recipient_user_id",
        "payment_user_id",
        "beneficiary_user_id",
        "requested_by",
        "created_by",
        "created_by_user_id",
        "operator_user_id",
        "changed_by",
        "updated_by",
        "admin_id",
        "related_user_id",
        "referred_id",
        "referrer_id",
        "recipient_id",
        "redeemed_by",
        "claimed_by",
    }
)


@dataclass(frozen=True)
class PrivacyPolicy:
    table: str
    ownership_columns: tuple[str, ...]
    disposition: Disposition
    reason: str
    anonymize_columns: tuple[str, ...] = ()
    anonymize_literals: tuple[tuple[str, str], ...] = ()
    required: bool = False


@dataclass(frozen=True)
class PrivacyManifestReport:
    ok: bool
    discovered_user_tables: tuple[str, ...]
    unknown_tables: tuple[str, ...]
    invalid_policies: tuple[str, ...]
    missing_required_tables: tuple[str, ...]


def _policy(
    table: str,
    columns: tuple[str, ...],
    disposition: Disposition,
    reason: str,
    *,
    anonymize: tuple[str, ...] = (),
    literals: tuple[tuple[str, str], ...] = (),
    required: bool = False,
) -> PrivacyPolicy:
    return PrivacyPolicy(
        table=table,
        ownership_columns=columns,
        disposition=disposition,
        reason=reason,
        anonymize_columns=anonymize,
        anonymize_literals=literals,
        required=required,
    )


# These tables are retired from the ClientPlatform runtime, but may still exist
# in historical production databases. Keeping their privacy disposition here is
# deliberately not a runtime dependency: it preserves export/erasure/accounting
# semantics until a separately governed retention migration removes the data.
_RETIRED_ERASE: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("pending_actions", ("user_id",), "historical pending interaction state"),
    ("deliveries", ("user_id",), "historical delivery schedule"),
    ("progress", ("user_id",), "historical progress state"),
    ("demo_events", ("user_id",), "historical demo behavior"),
    ("user_state_log", ("user_id",), "historical interaction diagnostics"),
    ("interaction_log", ("user_id",), "historical interaction timing"),
    ("user_behavior", ("user_id",), "historical derived behavior"),
    ("user_funnel", ("user_id",), "historical funnel state"),
    ("user_bricks", ("user_id",), "historical content exposure"),
    ("micro_answers", ("user_id",), "historical questionnaire answers"),
    ("ai_decisions", ("user_id",), "historical decision records"),
    ("selected_plan", ("user_id",), "historical plan choice"),
    ("weather_prefs", ("user_id",), "historical location preference"),
    ("user_settings", ("user_id",), "historical user settings"),
    ("mood_sessions", ("user_id",), "historical self-assessment"),
    ("state_ratings", ("user_id",), "historical self-assessment rating"),
    ("body_feedback", ("user_id",), "historical body feedback"),
    ("user_daily_state", ("user_id",), "historical daily state"),
    ("user_dynamic_profile", ("user_id",), "historical derived profile"),
    ("system_reactions_log", ("user_id",), "historical automated reaction"),
    ("sla_metrics", ("user_id",), "historical per-user telemetry"),
    ("decision_rewards", ("user_id",), "historical decision reward"),
    ("funnel_events", ("user_id",), "historical funnel event"),
    ("daily_audio_log", ("user_id",), "historical media delivery behavior"),
    ("gift_bonus_log", ("user_id",), "historical bonus behavior"),
    ("referrals", ("referred_id", "referrer_id"), "historical referral relation"),
    ("practice_token_audit", ("user_id",), "historical access audit"),
    ("trial_analytics", ("user_id",), "historical trial analytics"),
    ("audio_progress", ("user_id",), "historical media progress"),
    ("messenger_audio_progress", ("user_id",), "historical messenger media progress"),
    ("user_audio_progress", ("user_id",), "historical user media progress"),
    ("user_audio_timeline", ("user_id",), "historical media timeline"),
    ("user_audio_access_tokens", ("user_id",), "historical media access capability"),
    ("user_channel_links", ("user_id",), "historical cross-channel relation"),
    ("user_delivery_preferences", ("user_id",), "historical delivery preference"),
    ("account_audio_progress", ("account_id",), "historical account media progress"),
    ("account_audio_deliveries", ("account_id",), "historical account media delivery"),
    ("account_audio_completions", ("account_id",), "historical account media completion"),
    ("growth_conversion_outbox", ("user_id",), "historical conversion attribution"),
    ("growth_apply_review_confirmations", ("user_id",), "historical growth review state"),
    ("sales_desk_contacts", ("user_id",), "historical sales contact profile"),
    ("sales_desk_events", ("user_id",), "historical sales interaction"),
    ("sales_desk_tasks", ("user_id",), "historical sales follow-up"),
)

_RETIRED_RETAIN: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("subscriptions", ("user_id",), "historical purchased-access accounting fact"),
    ("payments", ("user_id",), "historical payment/refund/accounting fact"),
    ("payment_events", ("user_id",), "historical provider payment idempotency fact"),
    ("payment_reconciliation_retry", ("user_id",), "historical payment fulfilment audit"),
    ("gift_codes", ("created_by", "recipient_id", "redeemed_by", "claimed_by"), "historical gift accounting fact"),
    ("gift_claims", ("buyer_user_id", "recipient_user_id"), "historical paid gift ownership fact"),
    ("bonus_grants", ("user_id", "related_user_id"), "historical reward accounting provenance"),
    ("practice_wallets", ("user_id",), "historical purchased balance"),
    ("practice_ledger", ("user_id",), "historical immutable accounting ledger"),
    ("payment_token_grants", ("user_id",), "historical payment entitlement provenance"),
    ("practice_reservations", ("user_id",), "historical purchased balance reservation"),
    ("user_practice_preferences", ("user_id",), "historical purchased-access fulfilment setting"),
    ("practice_token_lots", ("user_id",), "historical payment-lot provenance"),
    ("premium_entitlements", ("user_id",), "historical purchased entitlement"),
    ("premium_delivery_outbox", ("user_id",), "historical purchased fulfilment evidence"),
    ("consultation_requests", ("user_id",), "historical paid consultation fulfilment"),
    ("telegram_stars_refunds", ("payment_user_id", "beneficiary_user_id", "requested_by"), "historical provider refund audit"),
    ("yookassa_refunds", ("user_id",), "historical provider refund audit"),
    ("sales_lead_revenue", ("user_id",), "historical currency-specific revenue fact"),
    ("growth_apply_requests", ("requested_by",), "historical administrative approval audit"),
    ("growth_apply_confirmations", ("admin_id",), "historical administrative confirmation audit"),
    ("user_roles", ("user_id",), "historical authorization assignment"),
    ("admin_permissions", ("admin_id", "updated_by"), "historical authorization audit"),
    ("plan_price_history", ("changed_by",), "historical pricing audit"),
    ("funnel_copies", ("created_by",), "historical administrative content authorship"),
)


_POLICIES = (
    _policy(
        "users",
        ("user_id",),
        "anonymize",
        "global messenger profile shell",
        anonymize=("username", "first_name"),
        required=True,
    ),
    _policy(
        "accounts",
        ("account_id", "primary_user_id"),
        "retain",
        "canonical cross-messenger account identity",
        required=True,
    ),
    _policy(
        "account_channel_identities",
        ("account_id",),
        "anonymize",
        "verified external routing identity retained for account continuity",
        anonymize=("username", "display_name"),
        required=True,
    ),
    _policy("user_channel_preferences", ("user_id",), "erase", "rebuildable channel preference"),
    _policy("user_channel_identities", ("user_id",), "erase", "rebuildable compatibility routing identity"),
    _policy(
        "user_channel_bridge_tokens",
        ("user_id", "account_id", "consumed_account_id"),
        "erase",
        "temporary cross-channel linking capability",
    ),
    _policy("user_privacy_export_tokens", ("user_id",), "erase", "one-time privacy export capability"),
    _policy("events", ("user_id",), "erase", "global user event history"),
    _policy("jobs", ("user_id",), "erase", "global user scheduled work"),
    _policy("messenger_delivery_outbox", ("canonical_user_id",), "erase", "outbound message payload and delivery state"),
    _policy("idempotency", ("user_id",), "erase", "shared runtime idempotency markers"),
    _policy("probe_runs", ("user_id",), "erase", "bounded operator probe evidence linked to a synthetic or real user"),
    _policy(
        "clientplatform_platform_operator_audit_events",
        ("operator_user_id",),
        "retain",
        "immutable high-trust platform operator lookup audit with no raw query payload",
        required=True,
    ),
    _policy("ad_oauth_sessions", ("user_id",), "erase", "short-lived advertising OAuth authorization state"),
    _policy("privacy_erasure_log", ("user_id",), "retain", "compliance evidence of an erasure request", required=True),
    _policy("businesses", ("created_by_user_id",), "retain", "tenant ownership provenance", required=True),
    _policy("business_members", ("user_id",), "retain", "tenant authorization and revocation audit", required=True),
    _policy(
        "clientplatform_owner_control_workspaces",
        ("user_id",),
        "erase",
        "owner-selected control workspace routing state",
        required=True,
    ),
    _policy(
        "clientplatform_owner_onboarding_sessions",
        ("user_id",),
        "erase",
        "short-lived owner onboarding continuation state",
        required=True,
    ),
    _policy(
        "clientplatform_owner_input_sessions",
        ("user_id",),
        "erase",
        "short-lived owner conversational input continuation state",
        required=True,
    ),
    *(_policy(table, columns, "erase", reason) for table, columns, reason in _RETIRED_ERASE),
    *(_policy(table, columns, "retain", reason) for table, columns, reason in _RETIRED_RETAIN),
    _policy(
        "sales_leads",
        ("user_id", "account_id"),
        "anonymize",
        "historical sales accounting retained without human-readable identity",
        anonymize=("username", "campaign", "creative", "closed_reason"),
        literals=(("display_name", "[deleted user]"),),
    ),
)

POLICIES: dict[str, PrivacyPolicy] = {policy.table: policy for policy in _POLICIES}
if len(POLICIES) != len(_POLICIES):
    raise RuntimeError("duplicate_privacy_manifest_table")


def _table_names(conn: Any) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {
        str(row["name"] if hasattr(row, "keys") else row[0])
        for row in rows
        if str(row["name"] if hasattr(row, "keys") else row[0])
        not in {"sqlite_sequence", "schema_migrations"}
    }


def table_columns(conn: Any, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608 - manifest-owned table names only
    except sqlite3.Error:
        return set()
    return {str(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}


def discovered_user_owned_tables(conn: Any) -> dict[str, tuple[str, ...]]:
    discovered: dict[str, tuple[str, ...]] = {}
    for table in sorted(_table_names(conn)):
        columns = table_columns(conn, table)
        # business_id is the explicit boundary for the tenant privacy manifest.
        # A small set of shared authorization/routing surfaces is intentionally
        # present in both manifests and must still be discovered globally.
        if "business_id" in columns and table not in POLICIES:
            continue
        ownership = tuple(sorted(columns & OWNERSHIP_COLUMN_CANDIDATES))
        if ownership:
            discovered[table] = ownership
    return discovered


def validate_privacy_manifest(conn: Any, *, strict: bool = True) -> PrivacyManifestReport:
    existing = _table_names(conn)
    discovered = discovered_user_owned_tables(conn)
    missing_required = tuple(
        sorted(policy.table for policy in POLICIES.values() if policy.required and policy.table not in existing)
    )
    invalid: list[str] = []
    for table in sorted(existing & set(POLICIES)):
        policy = POLICIES[table]
        columns = table_columns(conn, table)
        declared_present = tuple(
            column for column in policy.ownership_columns if column in columns
        )
        discovered_columns = discovered.get(table, ())
        if not declared_present:
            invalid.append(f"{table}:missing_declared_ownership_column")
        elif set(discovered_columns) - set(policy.ownership_columns):
            invalid.append(
                f"{table}:undeclared_ownership_columns="
                f"{','.join(sorted(set(discovered_columns) - set(policy.ownership_columns)))}"
            )
        declared_anonymize = set(policy.anonymize_columns) | {
            column for column, _value in policy.anonymize_literals
        }
        missing_anonymize = declared_anonymize - columns
        if missing_anonymize:
            invalid.append(
                f"{table}:missing_anonymize_columns={','.join(sorted(missing_anonymize))}"
            )
    invalid_policies = tuple(invalid)
    unknown = tuple(sorted(set(discovered) - set(POLICIES)))
    report = PrivacyManifestReport(
        ok=not unknown and not invalid and not missing_required,
        discovered_user_tables=tuple(sorted(discovered)),
        unknown_tables=unknown,
        invalid_policies=invalid_policies,
        missing_required_tables=missing_required,
    )
    if strict and not report.ok:
        parts: list[str] = []
        if unknown:
            parts.append(f"unknown={','.join(unknown)}")
        if invalid:
            parts.append(f"invalid={';'.join(invalid)}")
        if missing_required:
            parts.append(f"missing_required={','.join(missing_required)}")
        raise RuntimeError("privacy_manifest_invalid:" + "|".join(parts))
    return report


def policies_by_disposition(disposition: Disposition) -> tuple[PrivacyPolicy, ...]:
    return tuple(policy for policy in POLICIES.values() if policy.disposition == disposition)
