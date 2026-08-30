from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

Disposition = Literal["erase", "retain", "anonymize"]
MANIFEST_VERSION = "2026-08-05.v7"

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


_BEHAVIORAL: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("events", ("user_id",), "behavioral event history"),
    ("jobs", ("user_id",), "scheduled behavioral delivery state"),
    ("idempotency", ("user_id",), "behavioral command deduplication"),
    ("pending_actions", ("user_id",), "pending interactive state"),
    ("deliveries", ("user_id",), "historical delivery schedule"),
    ("probe_runs", ("user_id",), "synthetic or operational user probe evidence"),
    ("progress", ("user_id",), "audio progress state"),
    ("demo_events", ("user_id",), "demo behavior history"),
    ("user_state_log", ("user_id",), "interaction state diagnostics"),
    ("interaction_log", ("user_id",), "raw interaction timing history"),
    ("user_behavior", ("user_id",), "derived behavioral rhythm profile"),
    ("user_funnel", ("user_id",), "personalized funnel state"),
    ("user_bricks", ("user_id",), "personalized content exposure history"),
    ("micro_answers", ("user_id",), "personalized questionnaire answers"),
    ("ai_decisions", ("user_id",), "personalized decision history"),
    ("selected_plan", ("user_id",), "pre-purchase behavioral choice"),
    ("weather_prefs", ("user_id",), "location preference"),
    ("user_settings", ("user_id",), "location and UX settings"),
    ("mood_sessions", ("user_id",), "self-assessment history"),
    ("state_ratings", ("user_id",), "self-assessment history"),
    ("body_feedback", ("user_id",), "body feedback history"),
    ("user_daily_state", ("user_id",), "daily behavioral state"),
    ("user_dynamic_profile", ("user_id",), "derived behavioral profile"),
    ("system_reactions_log", ("user_id",), "automated behavioral reactions"),
    ("sla_metrics", ("user_id",), "per-user UX telemetry"),
    ("decision_rewards", ("user_id",), "per-user decision reward analytics"),
    ("funnel_events", ("user_id",), "marketing funnel behavior"),
    ("daily_audio_log", ("user_id",), "daily audio delivery behavior"),
    ("gift_bonus_log", ("user_id",), "gift marketing bonus history"),
    ("referrals", ("referred_id", "referrer_id"), "referral relationship graph"),
    ("practice_token_audit", ("user_id",), "behavioral access audit"),
    ("trial_analytics", ("user_id",), "trial behavior analytics"),
    ("audio_progress", ("user_id",), "legacy audio progress"),
    ("messenger_audio_progress", ("user_id",), "messenger audio progress"),
    ("user_audio_progress", ("user_id",), "legacy user audio progress"),
    ("user_audio_timeline", ("user_id",), "audio interaction timeline"),
    ("user_audio_access_tokens", ("user_id",), "expiring media access capability"),
    ("user_privacy_export_tokens", ("user_id",), "one-time privacy export capability"),
    ("user_channel_links", ("user_id",), "legacy cross-channel relationship"),
    ("user_channel_preferences", ("user_id",), "legacy delivery preference"),
    ("user_delivery_preferences", ("user_id",), "delivery preferences"),
    (
        "user_channel_bridge_tokens",
        ("user_id", "account_id", "consumed_account_id"),
        "temporary channel-link capabilities",
    ),
    ("account_audio_progress", ("account_id",), "canonical audio progress"),
    ("account_audio_deliveries", ("account_id",), "canonical audio delivery history"),
    ("account_audio_completions", ("account_id",), "canonical completion history"),
    ("messenger_delivery_outbox", ("canonical_user_id",), "message bodies and delivery attempts"),
    ("growth_conversion_outbox", ("user_id",), "marketing conversion attribution"),
    ("growth_apply_review_confirmations", ("user_id",), "legacy growth review state"),
    ("sales_desk_contacts", ("user_id",), "legacy sales contact profile"),
    ("sales_desk_events", ("user_id",), "legacy sales interaction history"),
    ("sales_desk_tasks", ("user_id",), "legacy sales follow-up state"),
    (
        "ad_oauth_sessions",
        ("user_id",),
        "short-lived advertising OAuth state and encrypted PKCE verifier",
    ),
)

_RETAINED: tuple[tuple[str, tuple[str, ...], str, bool], ...] = (
    ("subscriptions", ("user_id",), "legacy purchased-access fact", True),
    ("payments", ("user_id",), "payment, refund, dispute and accounting fact", True),
    ("payment_events", ("user_id",), "provider payment idempotency fact", True),
    (
        "payment_reconciliation_retry",
        ("user_id",),
        "provider-verified payment fulfilment retry and audit fact",
        True,
    ),
    (
        "gift_codes",
        ("created_by", "recipient_id", "redeemed_by", "claimed_by"),
        "gift accounting and ownership fact",
        False,
    ),
    ("gift_claims", ("buyer_user_id", "recipient_user_id"), "paid gift ownership and refund fact", True),
    (
        "bonus_grants",
        ("user_id", "related_user_id"),
        "reward entitlement, idempotency and accounting provenance",
        True,
    ),
    ("practice_wallets", ("user_id",), "current purchased balance", True),
    ("practice_ledger", ("user_id",), "immutable token accounting ledger", True),
    ("payment_token_grants", ("user_id",), "payment-to-entitlement provenance", True),
    ("practice_reservations", ("user_id",), "purchased-token reservation accounting", True),
    ("user_practice_preferences", ("user_id",), "fulfilment setting for purchased access", True),
    ("practice_token_lots", ("user_id",), "exact payment-lot provenance and refunds", True),
    ("premium_entitlements", ("user_id",), "purchased premium entitlement", True),
    ("premium_delivery_outbox", ("user_id",), "premium fulfilment evidence", True),
    ("consultation_requests", ("user_id",), "paid consultation fulfilment", True),
    (
        "telegram_stars_refunds",
        ("payment_user_id", "beneficiary_user_id", "requested_by"),
        "provider refund state and audit",
        True,
    ),
    ("yookassa_refunds", ("user_id",), "provider refund state and audit", True),
    ("sales_lead_revenue", ("user_id",), "currency-specific revenue accounting fact", True),
    ("privacy_erasure_log", ("user_id",), "compliance evidence that erasure occurred", True),
)

_POLICIES = (
    _policy(
        "users",
        ("user_id",),
        "anonymize",
        "profile shell retained for account continuity",
        anonymize=(
            "username",
            "first_name",
            "work_time",
            "home_time",
            "last_work_date",
            "last_home_date",
        ),
        required=True,
    ),
    _policy(
        "accounts",
        ("account_id", "primary_user_id"),
        "retain",
        "canonical paid-account routing",
        required=True,
    ),
    _policy(
        "account_channel_identities",
        ("account_id",),
        "anonymize",
        "external routing id retained for fulfilment",
        anonymize=("username", "display_name"),
        required=True,
    ),
    _policy(
        "user_channel_identities",
        ("user_id",),
        "anonymize",
        "legacy routing identity",
        anonymize=("username", "display_name"),
    ),
    _policy(
        "sales_leads",
        ("user_id", "account_id"),
        "anonymize",
        "revenue and stage audit retained without human-readable identity",
        anonymize=("username", "campaign", "creative", "closed_reason"),
        literals=(("display_name", "[deleted user]"),),
        required=True,
    ),
    _policy(
        "growth_apply_requests",
        ("requested_by",),
        "retain",
        "administrative approval and security audit",
    ),
    _policy(
        "growth_apply_confirmations",
        ("admin_id",),
        "retain",
        "administrative confirmation security audit",
    ),
    _policy(
        "businesses",
        ("created_by_user_id",),
        "retain",
        "tenant ownership and creation provenance",
        required=True,
    ),
    _policy(
        "business_members",
        ("user_id",),
        "retain",
        "tenant authorization assignment and revocation audit",
        required=True,
    ),
    _policy(
        "clientplatform_owner_control_workspaces",
        ("user_id",),
        "erase",
        "owner-selected control workspace routing state",
        required=True,
    ),
    *(_policy(table, columns, "erase", reason) for table, columns, reason in _BEHAVIORAL),
    *(
        _policy(table, columns, "retain", reason, required=required)
        for table, columns, reason, required in _RETAINED
    ),
    _policy("user_roles", ("user_id",), "retain", "authorization assignment"),
    _policy("admin_permissions", ("admin_id", "updated_by"), "retain", "authorization audit"),
    _policy("plan_price_history", ("changed_by",), "retain", "pricing audit"),
    _policy("funnel_copies", ("created_by",), "retain", "administrative content authorship"),
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
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608
    except sqlite3.Error:
        return set()
    return {str(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}


def discovered_user_owned_tables(conn: Any) -> dict[str, tuple[str, ...]]:
    discovered: dict[str, tuple[str, ...]] = {}
    for table in sorted(_table_names(conn)):
        columns = table_columns(conn, table)
        ownership = tuple(sorted(columns & OWNERSHIP_COLUMN_CANDIDATES))
        if ownership:
            discovered[table] = ownership
    return discovered


def validate_privacy_manifest(conn: Any, *, strict: bool = True) -> PrivacyManifestReport:
    existing = _table_names(conn)
    discovered = discovered_user_owned_tables(conn)
    unknown = tuple(sorted(set(discovered) - set(POLICIES)))
    missing_required = tuple(
        sorted(
            policy.table
            for policy in POLICIES.values()
            if policy.required and policy.table not in existing
        )
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

    report = PrivacyManifestReport(
        ok=not unknown and not invalid and not missing_required,
        discovered_user_tables=tuple(sorted(discovered)),
        unknown_tables=unknown,
        invalid_policies=tuple(invalid),
        missing_required_tables=missing_required,
    )
    if strict and not report.ok:
        parts: list[str] = []
        if report.unknown_tables:
            parts.append(f"unknown={','.join(report.unknown_tables)}")
        if report.invalid_policies:
            parts.append(f"invalid={';'.join(report.invalid_policies)}")
        if report.missing_required_tables:
            parts.append(f"missing_required={','.join(report.missing_required_tables)}")
        raise RuntimeError("privacy_manifest_invalid:" + "|".join(parts))
    return report


def policies_by_disposition(disposition: Disposition) -> tuple[PrivacyPolicy, ...]:
    return tuple(
        policy
        for policy in POLICIES.values()
        if policy.disposition == disposition
    )
