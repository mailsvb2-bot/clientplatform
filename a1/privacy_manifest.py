from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

TenantDisposition = Literal["erase", "retain", "anonymize"]
A1_PRIVACY_MANIFEST_VERSION = "2026-07-28.v2"


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
)

TENANT_POLICIES: dict[str, TenantPrivacyPolicy] = {
    policy.table: policy for policy in _POLICIES
}
if len(TENANT_POLICIES) != len(_POLICIES):
    raise RuntimeError("duplicate_a1_privacy_manifest_table")


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


def validate_a1_privacy_manifest(
    conn: Any,
    *,
    strict: bool = True,
) -> TenantPrivacyManifestReport:
    existing = _table_names(conn)
    discovered = discovered_business_scoped_tables(conn)
    unknown = tuple(sorted(set(discovered) - set(TENANT_POLICIES)))
    missing_required = tuple(
        sorted(
            policy.table
            for policy in TENANT_POLICIES.values()
            if policy.required and policy.table not in existing
        )
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
        raise RuntimeError("a1_privacy_manifest_invalid:" + "|".join(parts))
    return report
