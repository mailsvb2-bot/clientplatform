from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

Disposition = Literal["erase", "retain", "anonymize"]
MANIFEST_VERSION = "2026-08-31.v1-clientplatform-global"


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
    existing = _table_names(conn)
    ownership_columns = {
        column
        for policy in POLICIES.values()
        for column in policy.ownership_columns
    }
    discovered: dict[str, tuple[str, ...]] = {}
    for table in existing:
        columns = table_columns(conn, table)
        present = tuple(sorted(columns & ownership_columns))
        if present:
            discovered[table] = present
    return discovered


def validate_privacy_manifest(conn: Any, *, strict: bool = True) -> PrivacyManifestReport:
    existing = _table_names(conn)
    discovered = discovered_user_owned_tables(conn)
    missing_required = tuple(
        sorted(policy.table for policy in POLICIES.values() if policy.required and policy.table not in existing)
    )
    invalid = tuple(
        sorted(
            f"{table}:missing_declared_ownership_column"
            for table, policy in POLICIES.items()
            if table in existing and not any(column in table_columns(conn, table) for column in policy.ownership_columns)
        )
    )
    unknown = tuple(sorted(set(discovered) - set(POLICIES)))
    report = PrivacyManifestReport(
        ok=not unknown and not invalid and not missing_required,
        discovered_user_tables=tuple(sorted(discovered)),
        unknown_tables=unknown,
        invalid_policies=invalid,
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
