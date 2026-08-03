from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

TenantDisposition = Literal["erase", "retain", "anonymize"]
CLIENTPLATFORM_PRIVACY_MANIFEST_VERSION = "2026-08-03.v9"


@dataclass(frozen=True, slots=True)
class TenantPrivacyPolicy:
    table: str
    disposition: TenantDisposition
    reason: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class TenantPrivacyManifestReport:
    ok: bool
    discovered_business_tables: tuple[str, ...]
    unknown_tables: tuple[str, ...]
    missing_required_tables: tuple[str, ...]


_POLICIES = (
    TenantPrivacyPolicy(
        table="business_members",
        disposition="retain",
        reason="tenant authorization and revocation audit",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="customers",
        disposition="anonymize",
        reason="tenant customer profile",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="customer_identities",
        disposition="erase",
        reason="external routing identity and contact metadata",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="business_profiles",
        disposition="retain",
        reason="business-owned activity description and onboarding state",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="business_capabilities",
        disposition="retain",
        reason="business-owned enabled activity connectors",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="business_offerings",
        disposition="retain",
        reason="business-owned consultation, service and custom offerings",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="business_admin_settings",
        disposition="retain",
        reason="business-owned administrator configuration and automation settings",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="business_offering_prices",
        disposition="retain",
        reason="business-owned offering prices and commercial configuration",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="business_publications",
        disposition="retain",
        reason="business-owned publication drafts, schedules and delivery state",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="business_subscription_state",
        disposition="retain",
        reason="business subscription plan, limits and renewal state",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="business_payments",
        disposition="anonymize",
        reason=(
            "business financial ledger retained for accounting while customer linkage "
            "and free-form personal notes are anonymized"
        ),
        required=True,
    ),
    TenantPrivacyPolicy(
        table="customer_invites",
        disposition="erase",
        reason="expiring customer connection capability and claim routing",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="booking_slots",
        disposition="anonymize",
        reason="business availability and customer appointment fulfilment",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="programs",
        disposition="retain",
        reason="business-owned program definition",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="lessons",
        disposition="retain",
        reason="business-owned lesson definition and content reference",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="program_media_cleanup_queue",
        disposition="erase",
        reason=(
            "temporary tenant-owned object-storage cleanup reference and retry state; "
            "erasure removes the object reference and pending cleanup metadata"
        ),
        required=True,
    ),
    TenantPrivacyPolicy(
        table="enrollments",
        disposition="anonymize",
        reason="customer participation and fulfilment state",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="lesson_deliveries",
        disposition="anonymize",
        reason="customer delivery attempts and fulfilment evidence",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="lesson_progress",
        disposition="anonymize",
        reason="customer lesson progress state",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="connections",
        disposition="retain",
        reason="business integration ownership, permissions and secret references",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="managed_bots",
        disposition="retain",
        reason="business-owned bot identity and webhook secret reference",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="managed_bot_provisioning_requests",
        disposition="erase",
        reason=(
            "operator provisioning workflow, secret references and verification state; "
            "cancel removes references and tenant erasure removes the request"
        ),
        required=True,
    ),
    TenantPrivacyPolicy(
        table="bot_gateway_ingress_events",
        disposition="erase",
        reason=(
            "short-lived Telegram update payload, tenant route and replay evidence; "
            "payload_json is removed after processed or dead terminal state"
        ),
        required=True,
    ),
    TenantPrivacyPolicy(
        table="delivery_dispatch_outbox",
        disposition="erase",
        reason="provider routing, payload snapshot and customer delivery attempts",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="clientplatform_admin_alerts",
        disposition="retain",
        reason="tenant operational alerts and resolution history without message payloads",
        required=True,
    ),
    TenantPrivacyPolicy(
        table="clientplatform_admin_audit_events",
        disposition="anonymize",
        reason=(
            "security and administrator action evidence retained while actor identifiers "
            "and free-form details are anonymized"
        ),
        required=True,
    ),
    TenantPrivacyPolicy(
        table="clientplatform_admin_interaction_metrics",
        disposition="anonymize",
        reason=(
            "bounded performance telemetry retained while administrator identifiers, "
            "callback subjects and error details are anonymized"
        ),
        required=True,
    ),
)

TENANT_POLICIES: dict[str, TenantPrivacyPolicy] = {
    policy.table: policy for policy in _POLICIES
}
if len(TENANT_POLICIES) != len(_POLICIES):
    raise RuntimeError("duplicate_clientplatform_privacy_manifest_table")


def _table_names(conn: Any) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {
        str(row["name"] if hasattr(row, "keys") else row[0])
        for row in rows
        if str(row["name"] if hasattr(row, "keys") else row[0])
        not in {"sqlite_sequence", "schema_migrations"}
    }


def _table_columns(conn: Any, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608
    except sqlite3.Error:
        return set()
    return {str(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}


def discovered_business_scoped_tables(conn: Any) -> tuple[str, ...]:
    return tuple(
        sorted(
            table
            for table in _table_names(conn)
            if "business_id" in _table_columns(conn, table)
        )
    )


def validate_clientplatform_privacy_manifest(
    conn: Any,
    *,
    strict: bool = True,
    require_complete: bool = False,
) -> TenantPrivacyManifestReport:
    """Validate tenant-table policies.

    Unknown discovered tables always fail closed. ``require_complete`` is used
    by application startup after schema initialization; isolated schema tests
    may validate only the modules they intentionally created.
    """

    existing = _table_names(conn)
    discovered = discovered_business_scoped_tables(conn)
    unknown = tuple(sorted(set(discovered) - set(TENANT_POLICIES)))
    missing_required = (
        tuple(
            sorted(
                policy.table
                for policy in TENANT_POLICIES.values()
                if policy.required and policy.table not in existing
            )
        )
        if require_complete
        else ()
    )
    report = TenantPrivacyManifestReport(
        ok=not unknown and not missing_required,
        discovered_business_tables=discovered,
        unknown_tables=unknown,
        missing_required_tables=missing_required,
    )
    if strict and not report.ok:
        parts: list[str] = []
        if unknown:
            parts.append(f"unknown={','.join(unknown)}")
        if missing_required:
            parts.append(f"missing_required={','.join(missing_required)}")
        raise RuntimeError("clientplatform_privacy_manifest_invalid:" + "|".join(parts))
    return report
