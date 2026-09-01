from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

TenantDisposition = Literal["erase", "retain", "anonymize"]
CLIENTPLATFORM_PRIVACY_MANIFEST_VERSION = "2026-09-01.v40-owner-onboarding-session"


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


def _required(table: str, disposition: TenantDisposition, reason: str) -> TenantPrivacyPolicy:
    return TenantPrivacyPolicy(table=table, disposition=disposition, reason=reason, required=True)


_POLICIES = (
    _required("business_members", "retain", "tenant authorization and revocation audit"),
    _required("clientplatform_owner_control_workspaces", "erase", "owner-selected messenger control workspace routing state"),
    _required("clientplatform_owner_onboarding_sessions", "erase", "short-lived owner onboarding continuation state"),
    _required("customers", "anonymize", "tenant customer profile"),
    _required("customer_identities", "erase", "external routing identity and contact metadata"),
    _required("business_profiles", "retain", "business-owned activity description and onboarding state"),
    _required("business_capabilities", "retain", "business-owned enabled activity connectors"),
    _required("business_offerings", "retain", "business-owned consultation, service and custom offerings"),
    _required("business_admin_settings", "retain", "business-owned administrator configuration and legacy compatibility settings"),
    _required("clientplatform_automation_policies", "retain", "versioned business-owned automation limits and owner approval evidence"),
    _required("clientplatform_automation_action_approvals", "retain", "immutable business-owned automation action intent, policy binding and owner decision evidence"),
    _required("business_offering_prices", "retain", "business-owned offering prices and commercial configuration"),
    _required("business_publications", "retain", "business-owned publication drafts, schedules and delivery state"),
    _required("business_subscription_state", "retain", "business subscription plan, limits and renewal state"),
    _required("business_payments", "anonymize", "business financial ledger retained for accounting while customer linkage and free-form personal notes are anonymized"),
    _required("business_payment_outcome_evidence", "anonymize", "idempotent payment-to-outcome evidence retained for accounting while provider references are anonymized"),
    _required("customer_invites", "erase", "expiring customer connection capability and claim routing"),
    _required("booking_slots", "anonymize", "business availability and customer appointment fulfilment"),
    _required("business_outcome_events", "anonymize", "canonical business outcome ledger retained while customer linkage and free-form metadata are anonymized"),
    _required("promotion_campaigns", "retain", "business-owned advertising copy, source channel and campaign lifecycle without customer identity"),
    _required("promotion_source_aliases", "retain", "business-owned source routing aliases for exact creative and placement attribution without customer identity"),
    _required("promotion_events", "anonymize", "customer-linked campaign and exact-source attribution retained only after customer identity is anonymized"),
    _required("attribution_identities", "retain", "business-owned opaque acquisition identities retain source lineage without storing raw public tokens or customer identity"),
    _required("acquisition_touches", "erase", "customer-linked first-party acquisition touch provenance"),
    _required("attribution_links", "erase", "customer and booking links to first-party acquisition provenance"),
    _required("revenue_attributions", "anonymize", "versioned financial attribution evidence retained while direct customer linkage is anonymized"),
    _required("partner_campaigns", "retain", "business-owned partner acquisition goal and preparation policy"),
    _required("partner_candidates", "erase", "partner profiles can contain public business contact and relationship evidence"),
    _required("partner_content_packs", "erase", "candidate-specific prepared outreach and collaboration copy"),
    _required("partner_placements", "erase", "candidate-linked partner placement and publication evidence"),
    _required("partner_referral_events", "erase", "candidate-linked referral capability and attribution evidence"),
    _required("partner_reply_events", "erase", "partner inbound messages and authenticated provider reply evidence"),
    _required("partner_outreach_approvals", "anonymize", "owner approval evidence for exact public-business-contact outreach while recipient/payload fingerprints remain non-reversible"),
    _required("external_product_connectors", "retain", "business-owned external product integration and secret reference without raw secret material"),
    _required("external_product_event_receipts", "erase", "customer-linked verified external product event evidence and bounded metadata"),
    _required("ad_connections", "erase", "personal advertising account identity and encrypted OAuth material"),
    _required("ad_oauth_sessions", "erase", "short-lived one-time OAuth state and encrypted PKCE verifier"),
    _required("ad_managed_campaigns", "retain", "business-owned provider campaign binding and provisioning lifecycle without customer identity or credential material"),
    _required("ad_publication_jobs", "retain", "business-owned provider publication intent and bounded delivery evidence"),
    _required("ad_publication_assets", "erase", "user-provided or generated advertising media, local storage references and provider media identifiers"),
    _required("creative_variant_bindings", "anonymize", "business-owned creative selection and generation lineage retained while selecting member linkage is anonymized"),
    _required("creative_growth_trials", "anonymize", "business-owned creative trial configuration retained while creator linkage is anonymized"),
    _required("creative_growth_trial_variants", "retain", "business-owned deterministic creative allocation and exact source routing without customer identity"),
    _required("ad_spend_authorizations", "retain", "business-owned advertising limits, provider snapshot and authorization lifecycle"),
    _required("ad_spend_consent_receipts", "anonymize", "immutable consent terms and hashes with anonymized owner identifiers"),
    _required("ad_spend_operations", "retain", "idempotent launch and stop intent, bounded provider evidence and retry history"),
    _required("ad_audit_events", "anonymize", "security and spending audit retained while actor linkage is anonymized"),
    _required("programs", "retain", "business-owned program definition"),
    _required("lessons", "retain", "business-owned lesson definition and content reference"),
    _required("program_media_cleanup_queue", "erase", "temporary tenant-owned object-storage cleanup reference and retry state"),
    _required("enrollments", "anonymize", "customer participation and fulfilment state"),
    _required("lesson_deliveries", "anonymize", "customer delivery attempts and fulfilment evidence"),
    _required("lesson_progress", "anonymize", "customer lesson progress state"),
    _required("connections", "retain", "business integration ownership, permissions and secret references"),
    _required("connection_credentials", "erase", "tenant-scoped encrypted messenger/email provider and webhook credential material"),
    _required("messenger_ingress_routes", "erase", "tenant messenger callback route, connection binding and secret references"),
    _required("native_messenger_provisioning_leases", "erase", "short-lived tenant provider-account setup serialization state"),
    _required("messenger_connection_setup_sessions", "erase", "short-lived owner capability for secure Telegram/VK/MAX connection setup"),
    _required("customer_channel_link_tokens", "erase", "short-lived customer-linked cross-channel identity grant digest and routing metadata"),
    _required("managed_bots", "retain", "business-owned bot identity and webhook secret reference"),
    _required("managed_bot_credentials", "erase", "tenant-scoped encrypted Telegram bot credential material"),
    _required("managed_bot_provisioning_requests", "erase", "operator provisioning workflow, secret references and verification state"),
    _required("bot_gateway_ingress_events", "erase", "short-lived Telegram update payload, tenant route and replay evidence"),
    _required("delivery_dispatch_outbox", "erase", "provider routing, payload snapshot and customer delivery attempts"),
    _required("provider_dispatch_outbox", "erase", "provider routing, partner recipient, prepared payload and send-attempt evidence"),
    _required("clientplatform_admin_alerts", "retain", "tenant operational alerts and resolution history without message payloads"),
    _required("clientplatform_admin_audit_events", "anonymize", "administrator action evidence with anonymized actor identifiers and details"),
    _required("clientplatform_admin_interaction_metrics", "anonymize", "bounded telemetry with anonymized administrator identifiers and error details"),
    _required("clientplatform_sales_leads", "anonymize", "tenant sales opportunity projection linked to customer identity and source metadata"),
    _required("clientplatform_sales_events", "erase", "sales event payloads may contain customer conversation and attribution metadata"),
    _required("clientplatform_sales_action_plans", "erase", "customer-linked proposed sales actions and rationales"),
    _required("clientplatform_sales_conversation_state", "erase", "rebuildable customer-linked sales journey projection"),
    _required("clientplatform_sales_handoffs", "erase", "operator takeover context can contain customer conversation metadata"),
    _required("clientplatform_sales_followups", "erase", "owner-approved follow-up message text, customer routing linkage and delivery lifecycle"),
    _required("clientplatform_sales_contact_suppressions", "anonymize", "channel opt-out safety state retained while customer and actor linkage is anonymized"),
    _required("clientplatform_sales_ai_jobs", "erase", "customer-linked advisory AI queue state and retry evidence"),
    _required("clientplatform_sales_ai_heads", "erase", "per-lead advisory AI freshness cursor follows customer message processing state"),
    _required("clientplatform_sales_ai_consents", "retain", "business-owned AI consent target, epoch and data-mode audit state"),
    _required("clientplatform_sales_ai_analysis_projection", "erase", "latest customer-linked advisory AI analysis and verified offer snapshot"),
    _required("commercial_ladders", "retain", "business-owned commercial ladder configuration"),
    _required("commercial_ladder_steps", "retain", "business-owned commercial offer sequence and approval thresholds"),
)

TENANT_POLICIES: dict[str, TenantPrivacyPolicy] = {policy.table: policy for policy in _POLICIES}
if len(TENANT_POLICIES) != len(_POLICIES):
    raise RuntimeError("duplicate_clientplatform_privacy_manifest_table")


def _table_names(conn: Any) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {
        str(row["name"] if hasattr(row, "keys") else row[0])
        for row in rows
        if str(row["name"] if hasattr(row, "keys") else row[0]) not in {"sqlite_sequence", "schema_migrations"}
    }


def _table_columns(conn: Any, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608
    except sqlite3.Error:
        return set()
    return {str(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}


def discovered_business_scoped_tables(conn: Any) -> tuple[str, ...]:
    return tuple(sorted(table for table in _table_names(conn) if "business_id" in _table_columns(conn, table)))


def validate_clientplatform_privacy_manifest(conn: Any, *, strict: bool = True, require_complete: bool = False) -> TenantPrivacyManifestReport:
    existing = _table_names(conn)
    discovered = discovered_business_scoped_tables(conn)
    unknown = tuple(sorted(set(discovered) - set(TENANT_POLICIES)))
    missing_required = (
        tuple(sorted(policy.table for policy in TENANT_POLICIES.values() if policy.required and policy.table not in existing))
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
