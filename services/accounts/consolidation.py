from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from services.accounts.identity import (
    _account_row_in_conn,
    _resolve_canonical_account_id_in_conn,
    _resolve_canonical_user_id_in_conn,
)
from services.admin import is_platform_admin
from services.db import atomic_db, get_db_ro
from services.db.runtime import is_postgres_enabled


class AccountConsolidationPermissionDenied(PermissionError):
    """The caller is not an explicitly configured platform operator."""


class AccountConsolidationUnavailable(RuntimeError):
    """One of the requested account records cannot participate in consolidation."""


class AccountConsolidationConflict(RuntimeError):
    """The dry-run found an ambiguity or unsafe dependency."""


class AccountConsolidationStalePlan(AccountConsolidationConflict):
    """Persistent account state changed after the operator reviewed the plan."""


@dataclass(frozen=True, slots=True)
class AccountConsolidationDependency:
    table: str
    column: str
    policy: str
    source_rows: int
    target_rows: int


@dataclass(frozen=True, slots=True)
class AccountConsolidationAccessExpansion:
    business_id: str
    role: str
    status: str


@dataclass(frozen=True, slots=True)
class AccountConsolidationPlan:
    source_account_id: int
    target_account_id: int
    source_user_id: int
    target_user_id: int
    source_platforms: tuple[str, ...]
    target_platforms: tuple[str, ...]
    dependencies: tuple[AccountConsolidationDependency, ...]
    access_expansions: tuple[AccountConsolidationAccessExpansion, ...]
    blockers: tuple[str, ...]
    plan_fingerprint: str
    confirmation_code: str
    planned_at: str

    @property
    def can_apply(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class AccountConsolidationResult:
    operation_id: str
    source_account_id: int
    target_account_id: int
    source_user_id: int
    target_user_id: int
    plan_fingerprint: str
    applied_at: str
    idempotent_replay: bool


_INTERNAL_ID_COLUMN = re.compile(
    r"^(?:account_id|primary_user_id|canonical_user_id|consumed_account_id|merged_into_account_id|"
    r"(?:[a-z0-9_]+_)?user_id|admin_id|requested_by)$",
    re.IGNORECASE,
)
_EXTERNAL_ID_COLUMNS = {
    "external_user_id",
    "used_external_user_id",
    "created_from_external_user_id",
    "external_account_id",
}

# This is a dependency-policy ratchet, not an alternate identity model. Any new
# internal user/account reference carrying source rows blocks consolidation until
# its semantics are explicitly reviewed here.
_KNOWN_IDENTITY_POLICIES: dict[tuple[str, str], str] = {
    ("accounts", "account_id"): "authority",
    ("accounts", "primary_user_id"): "authority",
    ("accounts", "merged_into_account_id"): "authority",
    ("accounts", "merged_by_user_id"): "retain_audit_actor",
    ("account_channel_identities", "account_id"): "repoint",
    ("users", "user_id"): "retain_compatibility_shell",
    ("user_channel_preferences", "user_id"): "merge_preference",
    ("user_channel_identities", "user_id"): "repoint",
    ("user_channel_bridge_tokens", "user_id"): "repoint_unconsumed",
    ("user_channel_bridge_tokens", "account_id"): "repoint_unconsumed",
    ("user_channel_bridge_tokens", "consumed_account_id"): "retain_history",
    ("user_privacy_export_tokens", "user_id"): "repoint_unconsumed",
    ("business_members", "user_id"): "repoint_authorization",
    ("clientplatform_owner_control_workspaces", "user_id"): "repoint_owner_state",
    ("clientplatform_owner_onboarding_sessions", "user_id"): "repoint_owner_state",
    ("clientplatform_owner_input_sessions", "user_id"): "repoint_owner_state",
    ("jobs", "user_id"): "repoint_unlocked_active",
    ("messenger_delivery_outbox", "canonical_user_id"): "repoint_not_started",
    ("idempotency", "user_id"): "merge_dedup_ledger",
    ("ad_oauth_sessions", "user_id"): "block_unconsumed",
    ("events", "user_id"): "retain_history",
    ("privacy_erasure_log", "user_id"): "retain_history",
    ("probe_runs", "user_id"): "retain_history",
    ("businesses", "created_by_user_id"): "retain_history",
    ("ad_spend_consent_receipts", "actor_user_id"): "retain_history",
    ("clientplatform_admin_audit_events", "actor_user_id"): "retain_history",
    ("clientplatform_admin_interaction_metrics", "actor_user_id"): "retain_history",
    ("clientplatform_platform_operator_audit_events", "operator_user_id"): "retain_history",
    ("clientplatform_platform_support_audit_events", "operator_user_id"): "retain_history",
    ("clientplatform_platform_support_sessions", "operator_user_id"): "retain_history",
    ("clientplatform_platform_support_sessions", "revoked_by_user_id"): "retain_history",
    ("clientplatform_support_cases", "claimed_by_operator_user_id"): "retain_history",
    ("account_consolidation_operations", "operator_user_id"): "retain_evidence",
    ("account_consolidation_operations", "source_account_id"): "retain_evidence",
    ("account_consolidation_operations", "target_account_id"): "retain_evidence",
    ("account_consolidation_operations", "source_user_id"): "retain_evidence",
    ("account_consolidation_operations", "target_user_id"): "retain_evidence",
    ("account_consolidation_audit_events", "operator_user_id"): "retain_evidence",
    ("account_consolidation_audit_events", "source_user_id"): "retain_evidence",
    ("account_consolidation_audit_events", "target_user_id"): "retain_evidence",
}


def _clock(value: datetime | None) -> datetime:
    current = value or datetime.now(tz=UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    return current.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _positive_id(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if normalized <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return normalized


def _operator(value: object) -> int:
    operator_user_id = _positive_id(value, field="operator_user_id")
    if not is_platform_admin(operator_user_id):
        raise AccountConsolidationPermissionDenied("platform account consolidation access required")
    return operator_user_id


def _text(value: object, *, field: str, minimum: int, maximum: int) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split())
    if not minimum <= len(normalized) <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum} characters")
    return normalized


def _sha256_json(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {str(key): row[key] for key in row.keys()}
    raise TypeError("database row mapping is required")


def _schema_columns(conn: Any) -> dict[str, tuple[str, ...]]:
    if is_postgres_enabled():
        rows = conn.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema=current_schema()
            ORDER BY table_name, ordinal_position
            """.strip()
        ).fetchall()
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(str(row["table_name"]), []).append(str(row["column_name"]))
        return {table: tuple(columns) for table, columns in out.items()}

    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    out: dict[str, tuple[str, ...]] = {}
    for row in tables:
        table = str(row["name"] if hasattr(row, "keys") else row[0])
        columns = conn.execute(
            "SELECT name FROM pragma_table_info(?) ORDER BY cid",
            (table,),
        ).fetchall()
        out[table] = tuple(
            str(item["name"] if hasattr(item, "keys") else item[0])
            for item in columns
        )
    return out


def _count(conn: Any, table: str, column: str, value: int, extra_sql: str = "", params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(
        f'SELECT COUNT(*) AS c FROM "{table}" WHERE "{column}"=? {extra_sql}',  # nosec B608 - schema-derived validated identifier
        (int(value), *params),
    ).fetchone()
    return int(row["c"] if hasattr(row, "keys") else row[0])


def _rows(conn: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [_row_dict(row) for row in conn.execute(sql, params).fetchall()]


def _account_exact(conn: Any, account_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT account_id, primary_user_id, status, merged_into_account_id,
               created_at, updated_at, merged_at, merged_by_user_id, merge_reason
        FROM accounts WHERE account_id=? LIMIT 1
        """.strip(),
        (int(account_id),),
    ).fetchone()
    if row is None:
        raise AccountConsolidationUnavailable(f"account_id={int(account_id)} was not found")
    return _row_dict(row)


def _dependency_inventory(
    conn: Any,
    *,
    schema: dict[str, tuple[str, ...]],
    source_account_id: int,
    target_account_id: int,
    source_user_id: int,
    target_user_id: int,
) -> tuple[list[AccountConsolidationDependency], list[str]]:
    dependencies: list[AccountConsolidationDependency] = []
    blockers: list[str] = []
    for table in sorted(schema):
        for column in schema[table]:
            lowered = column.casefold()
            if lowered in _EXTERNAL_ID_COLUMNS or not _INTERNAL_ID_COLUMN.fullmatch(lowered):
                continue
            policy = _KNOWN_IDENTITY_POLICIES.get((table, column))
            account_scoped = "account_id" in lowered
            source_value = source_account_id if account_scoped else source_user_id
            target_value = target_account_id if account_scoped else target_user_id
            source_rows = _count(conn, table, column, source_value)
            target_rows = _count(conn, table, column, target_value)
            if policy is None:
                policy = "unknown_fail_closed"
                if source_rows:
                    blockers.append(f"unknown_identity_dependency:{table}.{column}:{source_rows}")
            dependencies.append(
                AccountConsolidationDependency(
                    table=table,
                    column=column,
                    policy=policy,
                    source_rows=source_rows,
                    target_rows=target_rows,
                )
            )
    return dependencies, blockers


def _owner_state(conn: Any, table: str, user_id: int, order_by: str) -> list[dict[str, Any]]:
    return _rows(
        conn,
        f'SELECT * FROM "{table}" WHERE user_id=? ORDER BY {order_by}',  # nosec B608 - internal constants only
        (int(user_id),),
    )


def _target_map(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[tuple[Any, ...], dict[str, Any]]:
    return {tuple(row[key] for key in keys): row for row in rows}


def _equivalent(row_a: dict[str, Any], row_b: dict[str, Any], ignored: set[str]) -> bool:
    keys = (set(row_a) | set(row_b)) - ignored
    return all(row_a.get(key) == row_b.get(key) for key in keys)


def _stable_state(
    conn: Any,
    *,
    source: dict[str, Any],
    target: dict[str, Any],
    schema: dict[str, tuple[str, ...]],
) -> tuple[dict[str, Any], list[str], list[AccountConsolidationAccessExpansion]]:
    source_account_id = int(source["account_id"])
    target_account_id = int(target["account_id"])
    source_user_id = int(source["primary_user_id"] if source["primary_user_id"] is not None else source_account_id)
    target_user_id = int(target["primary_user_id"] if target["primary_user_id"] is not None else target_account_id)
    blockers: list[str] = []

    if source_account_id == target_account_id:
        blockers.append("source_and_target_are_identical")
    if source_user_id == target_user_id:
        blockers.append("source_and_target_primary_user_are_identical")
    if str(source["status"]) != "active" or source["merged_into_account_id"] is not None:
        blockers.append("source_account_is_not_active_unmerged")
    if str(target["status"]) != "active" or target["merged_into_account_id"] is not None:
        blockers.append("target_account_is_not_active_unmerged")
    if is_platform_admin(source_user_id) or is_platform_admin(target_user_id):
        blockers.append("platform_admin_account_requires_out_of_band_owner_decision")

    source_identities = _rows(
        conn,
        """
        SELECT platform, external_user_id, verified_at, link_source, linked_at, last_seen_at
        FROM account_channel_identities WHERE account_id=? ORDER BY platform, external_user_id
        """.strip(),
        (source_account_id,),
    )
    target_identities = _rows(
        conn,
        """
        SELECT platform, external_user_id, verified_at, link_source, linked_at, last_seen_at
        FROM account_channel_identities WHERE account_id=? ORDER BY platform, external_user_id
        """.strip(),
        (target_account_id,),
    )
    source_platforms = {str(row["platform"]) for row in source_identities}
    target_platforms = {str(row["platform"]) for row in target_identities}
    for platform in sorted(source_platforms & target_platforms):
        blockers.append(f"account_channel_identity_collision:{platform}")

    source_legacy_identities = _rows(
        conn,
        """
        SELECT platform, external_user_id, username, display_name, first_seen_at, last_seen_at
        FROM user_channel_identities WHERE user_id=? ORDER BY platform, external_user_id
        """.strip(),
        (source_user_id,),
    )
    target_legacy_identities = _rows(
        conn,
        """
        SELECT platform, external_user_id, username, display_name, first_seen_at, last_seen_at
        FROM user_channel_identities WHERE user_id=? ORDER BY platform, external_user_id
        """.strip(),
        (target_user_id,),
    )
    source_legacy_platforms = {str(row["platform"]) for row in source_legacy_identities}
    target_legacy_platforms = {str(row["platform"]) for row in target_legacy_identities}
    for platform in sorted(source_legacy_platforms & target_legacy_platforms):
        blockers.append(f"legacy_channel_identity_collision:{platform}")

    source_memberships = _rows(
        conn,
        """
        SELECT id, business_id, user_id, role, status, created_at, updated_at, revoked_at
        FROM business_members WHERE user_id=? ORDER BY business_id, created_at, id
        """.strip(),
        (source_user_id,),
    )
    target_memberships = _rows(
        conn,
        """
        SELECT id, business_id, user_id, role, status, created_at, updated_at, revoked_at
        FROM business_members WHERE user_id=? ORDER BY business_id, created_at, id
        """.strip(),
        (target_user_id,),
    )
    target_businesses = {str(row["business_id"]): row for row in target_memberships}
    access_expansions: list[AccountConsolidationAccessExpansion] = []
    for membership in source_memberships:
        business_id = str(membership["business_id"])
        if business_id in target_businesses:
            other = target_businesses[business_id]
            blockers.append(
                "membership_overlap:"
                f"{business_id}:source={membership['role']}/{membership['status']}:"
                f"target={other['role']}/{other['status']}"
            )
        elif str(membership["status"]) == "active":
            access_expansions.append(
                AccountConsolidationAccessExpansion(
                    business_id=business_id,
                    role=str(membership["role"]),
                    status=str(membership["status"]),
                )
            )

    source_workspaces = _owner_state(
        conn, "clientplatform_owner_control_workspaces", source_user_id, "platform"
    )
    target_workspaces = _target_map(
        _owner_state(conn, "clientplatform_owner_control_workspaces", target_user_id, "platform"),
        ("platform",),
    )
    for row in source_workspaces:
        key = (row["platform"],)
        other = target_workspaces.get(key)
        if other is not None and str(other["business_id"]) != str(row["business_id"]):
            blockers.append(f"owner_workspace_collision:{row['platform']}")

    source_inputs = _owner_state(
        conn, "clientplatform_owner_input_sessions", source_user_id, "platform, surface"
    )
    target_inputs = _target_map(
        _owner_state(conn, "clientplatform_owner_input_sessions", target_user_id, "platform, surface"),
        ("platform", "surface"),
    )
    for row in source_inputs:
        key = (row["platform"], row["surface"])
        other = target_inputs.get(key)
        if other is not None and not _equivalent(row, other, {"user_id", "updated_at"}):
            blockers.append(f"owner_input_collision:{row['platform']}:{row['surface']}")

    source_onboarding = _owner_state(
        conn, "clientplatform_owner_onboarding_sessions", source_user_id, "platform"
    )
    target_onboarding = _target_map(
        _owner_state(conn, "clientplatform_owner_onboarding_sessions", target_user_id, "platform"),
        ("platform",),
    )
    for row in source_onboarding:
        key = (row["platform"],)
        other = target_onboarding.get(key)
        if other is not None and not _equivalent(row, other, {"user_id", "updated_at"}):
            blockers.append(f"owner_onboarding_collision:{row['platform']}")

    active_oauth = _rows(
        conn,
        """
        SELECT state_hash, business_id, provider, expires_at, consumed_at,
               completion_attempt_id, completion_attempt_expires_at
        FROM ad_oauth_sessions WHERE user_id=? AND consumed_at IS NULL ORDER BY state_hash
        """.strip(),
        (source_user_id,),
    )
    if active_oauth:
        blockers.append(f"active_ad_oauth_sessions:{len(active_oauth)}")

    active_jobs = _rows(
        conn,
        """
        SELECT id, job_type, job_key, run_at_utc, locked_at, lock_token, done_at
        FROM jobs WHERE user_id=? AND done_at IS NULL ORDER BY id
        """.strip(),
        (source_user_id,),
    )
    locked_jobs = [row for row in active_jobs if row["locked_at"] is not None]
    if locked_jobs:
        blockers.append(f"locked_active_jobs:{len(locked_jobs)}")

    active_outbox = _rows(
        conn,
        """
        SELECT id, platform, event_key, status, locked_at, lock_token, available_at
        FROM messenger_delivery_outbox
        WHERE canonical_user_id=? AND status NOT IN ('sent','dead')
        ORDER BY id
        """.strip(),
        (source_user_id,),
    )
    sending = [row for row in active_outbox if str(row["status"]) == "sending"]
    if sending:
        blockers.append(f"sending_outbox_rows:{len(sending)}")
    unknown_outbox = [
        row for row in active_outbox if str(row["status"]) not in {"pending", "retry", "sending"}
    ]
    if unknown_outbox:
        blockers.append(f"unknown_active_outbox_status:{len(unknown_outbox)}")

    source_pref = _rows(
        conn,
        "SELECT * FROM user_channel_preferences WHERE user_id=?",
        (source_user_id,),
    )
    target_pref = _rows(
        conn,
        "SELECT * FROM user_channel_preferences WHERE user_id=?",
        (target_user_id,),
    )
    bridge_open = _rows(
        conn,
        """
        SELECT token, user_id, account_id, target_platform, created_at, expires_at
        FROM user_channel_bridge_tokens WHERE used_at IS NULL AND (user_id=? OR account_id=?)
        ORDER BY token
        """.strip(),
        (source_user_id, source_account_id),
    )
    bridge_state = [
        {**row, "token": hashlib.sha256(str(row["token"]).encode("utf-8")).hexdigest()}
        for row in bridge_open
    ]
    privacy_open = _rows(
        conn,
        """
        SELECT token_hash, platform, created_at, consumed_at
        FROM user_privacy_export_tokens WHERE user_id=? AND consumed_at IS NULL ORDER BY token_hash
        """.strip(),
        (source_user_id,),
    )
    dedup = _rows(
        conn,
        "SELECT key, created_at FROM idempotency WHERE user_id=? ORDER BY key",
        (source_user_id,),
    )
    dedup_state = [
        {"key_hash": hashlib.sha256(str(row["key"]).encode("utf-8")).hexdigest(), "created_at": row["created_at"]}
        for row in dedup
    ]

    state = {
        "schema_identity_columns": sorted(
            f"{table}.{column}"
            for table, columns in schema.items()
            for column in columns
            if column.casefold() not in _EXTERNAL_ID_COLUMNS
            and _INTERNAL_ID_COLUMN.fullmatch(column.casefold())
        ),
        "source_account": source,
        "target_account": target,
        "source_identities": source_identities,
        "target_identities": target_identities,
        "source_legacy_identities": source_legacy_identities,
        "target_legacy_identities": target_legacy_identities,
        "source_memberships": source_memberships,
        "target_memberships": target_memberships,
        "source_workspaces": source_workspaces,
        "target_workspaces": list(target_workspaces.values()),
        "source_inputs": source_inputs,
        "target_inputs": list(target_inputs.values()),
        "source_onboarding": source_onboarding,
        "target_onboarding": list(target_onboarding.values()),
        "active_oauth": active_oauth,
        "active_jobs": active_jobs,
        "active_outbox": active_outbox,
        "source_preference": source_pref,
        "target_preference": target_pref,
        "open_bridge_tokens": bridge_state,
        "open_privacy_tokens": privacy_open,
        "idempotency": dedup_state,
    }
    return state, blockers, access_expansions


def _serialize_dependencies(
    dependencies: list[AccountConsolidationDependency] | tuple[AccountConsolidationDependency, ...],
) -> list[dict[str, object]]:
    return [
        {
            "table": item.table,
            "column": item.column,
            "policy": item.policy,
            "source_rows": item.source_rows,
            "target_rows": item.target_rows,
        }
        for item in dependencies
    ]


def _build_plan_in_conn(
    conn: Any,
    *,
    source_account_id: int,
    target_account_id: int,
    planned_at: str,
) -> AccountConsolidationPlan:
    source = _account_exact(conn, source_account_id)
    target = _account_exact(conn, target_account_id)
    schema = _schema_columns(conn)
    source_user_id = int(
        source["primary_user_id"]
        if source["primary_user_id"] is not None
        else source["account_id"]
    )
    target_user_id = int(
        target["primary_user_id"]
        if target["primary_user_id"] is not None
        else target["account_id"]
    )
    dependencies, dependency_blockers = _dependency_inventory(
        conn,
        schema=schema,
        source_account_id=int(source["account_id"]),
        target_account_id=int(target["account_id"]),
        source_user_id=source_user_id,
        target_user_id=target_user_id,
    )
    state, state_blockers, access_expansions = _stable_state(
        conn,
        source=source,
        target=target,
        schema=schema,
    )
    blockers = tuple(sorted(set(dependency_blockers + state_blockers)))
    fingerprint = _sha256_json(
        {
            "contract_version": 1,
            "state": state,
            "dependencies": _serialize_dependencies(dependencies),
            "access_expansions": [
                {
                    "business_id": item.business_id,
                    "role": item.role,
                    "status": item.status,
                }
                for item in access_expansions
            ],
            "blockers": blockers,
        }
    )
    source_platforms = tuple(
        sorted(str(row["platform"]) for row in state["source_identities"])
    )
    target_platforms = tuple(
        sorted(str(row["platform"]) for row in state["target_identities"])
    )
    confirmation_code = (
        f"MERGE-{int(source['account_id'])}-TO-{int(target['account_id'])}-{fingerprint[:12]}"
    )
    return AccountConsolidationPlan(
        source_account_id=int(source["account_id"]),
        target_account_id=int(target["account_id"]),
        source_user_id=source_user_id,
        target_user_id=target_user_id,
        source_platforms=source_platforms,
        target_platforms=target_platforms,
        dependencies=tuple(dependencies),
        access_expansions=tuple(access_expansions),
        blockers=blockers,
        plan_fingerprint=fingerprint,
        confirmation_code=confirmation_code,
        planned_at=planned_at,
    )


def plan_account_consolidation(
    operator_user_id: int | None,
    *,
    source_account_id: int,
    target_account_id: int,
    now_utc: datetime | None = None,
) -> AccountConsolidationPlan:
    _operator(operator_user_id)
    source_id = _positive_id(source_account_id, field="source_account_id")
    target_id = _positive_id(target_account_id, field="target_account_id")
    if source_id == target_id:
        raise ValueError("source_account_id and target_account_id must differ")
    planned_at = _stamp(_clock(now_utc))
    with get_db_ro() as conn:
        return _build_plan_in_conn(
            conn,
            source_account_id=source_id,
            target_account_id=target_id,
            planned_at=planned_at,
        )


def _request_fingerprint(
    *, source_account_id: int, target_account_id: int, reason: str
) -> str:
    return _sha256_json(
        {
            "source_account_id": source_account_id,
            "target_account_id": target_account_id,
            "reason": reason,
        }
    )


def _operation_id(operator_user_id: int, idempotency_key: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"clientplatform:account-consolidation:{operator_user_id}:{idempotency_key}",
        )
    )


def _load_operation(
    conn: Any,
    *,
    operator_user_id: int,
    idempotency_key: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, operator_user_id, source_account_id, target_account_id,
               source_user_id, target_user_id, idempotency_key,
               request_fingerprint, plan_fingerprint, reason, status,
               created_at, applied_at, before_counts_json, after_counts_json
        FROM account_consolidation_operations
        WHERE operator_user_id=? AND idempotency_key=?
        LIMIT 1
        """.strip(),
        (operator_user_id, idempotency_key),
    ).fetchone()
    return None if row is None else _row_dict(row)


def _result_from_operation(
    operation: dict[str, Any], *, replay: bool
) -> AccountConsolidationResult:
    return AccountConsolidationResult(
        operation_id=str(operation["id"]),
        source_account_id=int(operation["source_account_id"]),
        target_account_id=int(operation["target_account_id"]),
        source_user_id=int(operation["source_user_id"]),
        target_user_id=int(operation["target_user_id"]),
        plan_fingerprint=str(operation["plan_fingerprint"]),
        applied_at=str(operation["applied_at"]),
        idempotent_replay=replay,
    )


def _lock_accounts(
    conn: Any, *, source_account_id: int, target_account_id: int
) -> None:
    for account_id in sorted((source_account_id, target_account_id)):
        cursor = conn.execute(
            "UPDATE accounts SET updated_at=updated_at WHERE account_id=?",
            (account_id,),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise AccountConsolidationUnavailable(
                f"account_id={account_id} became unavailable"
            )


def _merge_preference(
    conn: Any, *, source_user_id: int, target_user_id: int, applied_at: str
) -> None:
    source = conn.execute(
        """
        SELECT preferred_platform, last_seen_platform, updated_at
        FROM user_channel_preferences WHERE user_id=?
        """.strip(),
        (source_user_id,),
    ).fetchone()
    if source is None:
        return
    target = conn.execute(
        """
        SELECT preferred_platform, last_seen_platform, updated_at
        FROM user_channel_preferences WHERE user_id=?
        """.strip(),
        (target_user_id,),
    ).fetchone()
    winner = source
    if target is not None and str(target["updated_at"] or "") >= str(source["updated_at"] or ""):
        winner = target
    conn.execute(
        """
        INSERT INTO user_channel_preferences(
            user_id, preferred_platform, last_seen_platform, updated_at
        ) VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            preferred_platform=excluded.preferred_platform,
            last_seen_platform=excluded.last_seen_platform,
            updated_at=excluded.updated_at
        """.strip(),
        (
            target_user_id,
            winner["preferred_platform"],
            winner["last_seen_platform"],
            applied_at,
        ),
    )
    conn.execute("DELETE FROM user_channel_preferences WHERE user_id=?", (source_user_id,))


def _move_owner_state(
    conn: Any, *, source_user_id: int, target_user_id: int
) -> None:
    control = _rows(
        conn,
        """
        SELECT platform, business_id, updated_at
        FROM clientplatform_owner_control_workspaces
        WHERE user_id=? ORDER BY platform
        """.strip(),
        (source_user_id,),
    )
    inputs = _rows(
        conn,
        """
        SELECT platform, surface, business_id, action, context_json, updated_at
        FROM clientplatform_owner_input_sessions
        WHERE user_id=? ORDER BY platform, surface
        """.strip(),
        (source_user_id,),
    )
    onboarding = _rows(
        conn,
        """
        SELECT platform, step, business_id, updated_at
        FROM clientplatform_owner_onboarding_sessions
        WHERE user_id=? ORDER BY platform
        """.strip(),
        (source_user_id,),
    )

    # The first two tables have a composite FK to business_members. Remove the
    # source-keyed children before repointing the stable membership row.
    conn.execute(
        "DELETE FROM clientplatform_owner_control_workspaces WHERE user_id=?",
        (source_user_id,),
    )
    conn.execute(
        "DELETE FROM clientplatform_owner_input_sessions WHERE user_id=?",
        (source_user_id,),
    )
    conn.execute(
        "DELETE FROM clientplatform_owner_onboarding_sessions WHERE user_id=?",
        (source_user_id,),
    )
    conn.execute(
        "UPDATE business_members SET user_id=? WHERE user_id=?",
        (target_user_id, source_user_id),
    )

    for row in control:
        conn.execute(
            """
            INSERT INTO clientplatform_owner_control_workspaces(
                user_id, platform, business_id, updated_at
            ) VALUES(?,?,?,?)
            ON CONFLICT(user_id, platform) DO NOTHING
            """.strip(),
            (target_user_id, row["platform"], row["business_id"], row["updated_at"]),
        )
    for row in inputs:
        conn.execute(
            """
            INSERT INTO clientplatform_owner_input_sessions(
                user_id, platform, surface, business_id, action, context_json, updated_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id, platform, surface) DO NOTHING
            """.strip(),
            (
                target_user_id,
                row["platform"],
                row["surface"],
                row["business_id"],
                row["action"],
                row["context_json"],
                row["updated_at"],
            ),
        )
    for row in onboarding:
        conn.execute(
            """
            INSERT INTO clientplatform_owner_onboarding_sessions(
                user_id, platform, step, business_id, updated_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(user_id, platform) DO NOTHING
            """.strip(),
            (
                target_user_id,
                row["platform"],
                row["step"],
                row["business_id"],
                row["updated_at"],
            ),
        )


def _merge_idempotency(
    conn: Any, *, source_user_id: int, target_user_id: int
) -> None:
    rows = conn.execute(
        "SELECT key, created_at FROM idempotency WHERE user_id=? ORDER BY key",
        (source_user_id,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT OR IGNORE INTO idempotency(user_id, key, created_at) VALUES(?,?,?)",
            (target_user_id, row["key"], row["created_at"]),
        )
    conn.execute("DELETE FROM idempotency WHERE user_id=?", (source_user_id,))


def _apply_mutations(
    conn: Any,
    *,
    plan: AccountConsolidationPlan,
    operator_user_id: int,
    reason: str,
    applied_at: str,
) -> None:
    source_account_id = plan.source_account_id
    target_account_id = plan.target_account_id
    source_user_id = plan.source_user_id
    target_user_id = plan.target_user_id

    _move_owner_state(
        conn,
        source_user_id=source_user_id,
        target_user_id=target_user_id,
    )
    conn.execute(
        "UPDATE account_channel_identities SET account_id=? WHERE account_id=?",
        (target_account_id, source_account_id),
    )
    conn.execute(
        "UPDATE user_channel_identities SET user_id=? WHERE user_id=?",
        (target_user_id, source_user_id),
    )
    _merge_preference(
        conn,
        source_user_id=source_user_id,
        target_user_id=target_user_id,
        applied_at=applied_at,
    )
    conn.execute(
        """
        UPDATE user_channel_bridge_tokens
        SET user_id=?, account_id=?
        WHERE used_at IS NULL AND (user_id=? OR account_id=?)
        """.strip(),
        (target_user_id, target_account_id, source_user_id, source_account_id),
    )
    conn.execute(
        """
        UPDATE user_privacy_export_tokens SET user_id=?
        WHERE user_id=? AND consumed_at IS NULL
        """.strip(),
        (target_user_id, source_user_id),
    )
    conn.execute(
        """
        UPDATE jobs SET user_id=?
        WHERE user_id=? AND done_at IS NULL AND locked_at IS NULL
        """.strip(),
        (target_user_id, source_user_id),
    )
    conn.execute(
        """
        UPDATE messenger_delivery_outbox SET canonical_user_id=?, updated_at=?
        WHERE canonical_user_id=? AND status IN ('pending','retry')
        """.strip(),
        (target_user_id, applied_at, source_user_id),
    )
    _merge_idempotency(
        conn,
        source_user_id=source_user_id,
        target_user_id=target_user_id,
    )
    cursor = conn.execute(
        """
        UPDATE accounts
        SET status='merged', merged_into_account_id=?, merged_at=?,
            merged_by_user_id=?, merge_reason=?, updated_at=?
        WHERE account_id=? AND status='active' AND merged_into_account_id IS NULL
        """.strip(),
        (
            target_account_id,
            applied_at,
            operator_user_id,
            reason,
            applied_at,
            source_account_id,
        ),
    )
    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
        raise AccountConsolidationStalePlan("source account changed before merge marker")
    conn.execute(
        "UPDATE accounts SET updated_at=? WHERE account_id=? AND status='active'",
        (applied_at, target_account_id),
    )


def _postcondition_counts(
    conn: Any, *, plan: AccountConsolidationPlan
) -> dict[str, int]:
    source_account_id = plan.source_account_id
    source_user_id = plan.source_user_id
    checks = {
        "account_channel_identities": _count(
            conn, "account_channel_identities", "account_id", source_account_id
        ),
        "business_members": _count(conn, "business_members", "user_id", source_user_id),
        "owner_control_workspaces": _count(
            conn, "clientplatform_owner_control_workspaces", "user_id", source_user_id
        ),
        "owner_input_sessions": _count(
            conn, "clientplatform_owner_input_sessions", "user_id", source_user_id
        ),
        "owner_onboarding_sessions": _count(
            conn, "clientplatform_owner_onboarding_sessions", "user_id", source_user_id
        ),
        "legacy_channel_identities": _count(
            conn, "user_channel_identities", "user_id", source_user_id
        ),
        "channel_preferences": _count(
            conn, "user_channel_preferences", "user_id", source_user_id
        ),
        "open_bridge_tokens_by_user": _count(
            conn,
            "user_channel_bridge_tokens",
            "user_id",
            source_user_id,
            "AND used_at IS NULL",
        ),
        "open_bridge_tokens_by_account": _count(
            conn,
            "user_channel_bridge_tokens",
            "account_id",
            source_account_id,
            "AND used_at IS NULL",
        ),
        "open_privacy_tokens": _count(
            conn,
            "user_privacy_export_tokens",
            "user_id",
            source_user_id,
            "AND consumed_at IS NULL",
        ),
        "active_jobs": _count(
            conn, "jobs", "user_id", source_user_id, "AND done_at IS NULL"
        ),
        "active_outbox": _count(
            conn,
            "messenger_delivery_outbox",
            "canonical_user_id",
            source_user_id,
            "AND status NOT IN ('sent','dead')",
        ),
        "idempotency": _count(conn, "idempotency", "user_id", source_user_id),
        "active_oauth": _count(
            conn,
            "ad_oauth_sessions",
            "user_id",
            source_user_id,
            "AND consumed_at IS NULL",
        ),
    }
    source = _account_exact(conn, source_account_id)
    checks["source_merge_marker"] = int(
        str(source["status"]) != "merged"
        or source["merged_into_account_id"] is None
        or int(source["merged_into_account_id"]) != plan.target_account_id
    )
    canonical = _resolve_canonical_account_id_in_conn(conn, source_account_id)
    checks["canonical_resolution"] = int(canonical != plan.target_account_id)
    return checks


def _before_counts(plan: AccountConsolidationPlan) -> dict[str, int]:
    counts = {
        f"{item.table}.{item.column}": item.source_rows
        for item in plan.dependencies
        if item.source_rows
    }
    counts["access_expansions"] = len(plan.access_expansions)
    return counts


def apply_account_consolidation(
    operator_user_id: int | None,
    *,
    source_account_id: int,
    target_account_id: int,
    expected_plan_fingerprint: str,
    confirmation_code: str,
    idempotency_key: str,
    reason: str,
    now_utc: datetime | None = None,
) -> AccountConsolidationResult:
    operator = _operator(operator_user_id)
    source_id = _positive_id(source_account_id, field="source_account_id")
    target_id = _positive_id(target_account_id, field="target_account_id")
    if source_id == target_id:
        raise ValueError("source_account_id and target_account_id must differ")
    expected = str(expected_plan_fingerprint or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("expected_plan_fingerprint must be a SHA-256 hex digest")
    normalized_confirmation = _text(
        confirmation_code, field="confirmation_code", minimum=10, maximum=120
    )
    operation_key = _text(
        idempotency_key, field="idempotency_key", minimum=1, maximum=200
    )
    normalized_reason = _text(reason, field="reason", minimum=3, maximum=500)
    applied_at = _stamp(_clock(now_utc))
    request_fingerprint = _request_fingerprint(
        source_account_id=source_id,
        target_account_id=target_id,
        reason=normalized_reason,
    )

    with atomic_db() as conn:
        existing = _load_operation(
            conn,
            operator_user_id=operator,
            idempotency_key=operation_key,
        )
        if existing is not None:
            if str(existing["request_fingerprint"]) != request_fingerprint:
                raise AccountConsolidationConflict(
                    "idempotency key was already used for a different consolidation request"
                )
            if str(existing["plan_fingerprint"]) != expected:
                raise AccountConsolidationConflict(
                    "idempotency replay does not match expected plan fingerprint"
                )
            return _result_from_operation(existing, replay=True)

        _lock_accounts(
            conn,
            source_account_id=source_id,
            target_account_id=target_id,
        )
        # A concurrent retry with the same operation key may have committed while
        # this transaction waited for the account row locks. Re-read the durable
        # operation after serialization so the loser returns the exact applied
        # result instead of misclassifying the already-completed merge as stale.
        existing = _load_operation(
            conn,
            operator_user_id=operator,
            idempotency_key=operation_key,
        )
        if existing is not None:
            if str(existing["request_fingerprint"]) != request_fingerprint:
                raise AccountConsolidationConflict(
                    "idempotency key was already used for a different consolidation request"
                )
            if str(existing["plan_fingerprint"]) != expected:
                raise AccountConsolidationConflict(
                    "idempotency replay does not match expected plan fingerprint"
                )
            return _result_from_operation(existing, replay=True)

        current_plan = _build_plan_in_conn(
            conn,
            source_account_id=source_id,
            target_account_id=target_id,
            planned_at=applied_at,
        )
        if current_plan.blockers:
            raise AccountConsolidationConflict(
                "account consolidation is blocked: " + ",".join(current_plan.blockers)
            )
        if current_plan.plan_fingerprint != expected:
            raise AccountConsolidationStalePlan(
                "account consolidation state changed after dry-run"
            )
        if current_plan.confirmation_code != normalized_confirmation:
            raise AccountConsolidationConflict(
                "confirmation code does not match the reviewed dry-run plan"
            )

        before_counts = _before_counts(current_plan)
        _apply_mutations(
            conn,
            plan=current_plan,
            operator_user_id=operator,
            reason=normalized_reason,
            applied_at=applied_at,
        )
        after_counts = _postcondition_counts(conn, plan=current_plan)
        remaining = {key: value for key, value in after_counts.items() if value}
        if remaining:
            raise AccountConsolidationConflict(
                "account consolidation postcondition failed: "
                + ",".join(f"{key}={value}" for key, value in sorted(remaining.items()))
            )

        operation_id = _operation_id(operator, operation_key)
        conn.execute(
            """
            INSERT INTO account_consolidation_operations(
                id, operator_user_id, source_account_id, target_account_id,
                source_user_id, target_user_id, idempotency_key,
                request_fingerprint, plan_fingerprint, reason, status,
                created_at, applied_at, before_counts_json, after_counts_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'applied',?,?,?,?)
            """.strip(),
            (
                operation_id,
                operator,
                current_plan.source_account_id,
                current_plan.target_account_id,
                current_plan.source_user_id,
                current_plan.target_user_id,
                operation_key,
                request_fingerprint,
                current_plan.plan_fingerprint,
                normalized_reason,
                applied_at,
                applied_at,
                json.dumps(before_counts, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                json.dumps(after_counts, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            ),
        )
        audit_id = str(
            uuid5(
                NAMESPACE_URL,
                f"clientplatform:account-consolidation-audit:{operation_id}:applied",
            )
        )
        conn.execute(
            """
            INSERT INTO account_consolidation_audit_events(
                id, operation_id, operator_user_id, source_user_id,
                target_user_id, event_type, detail_json, created_at
            ) VALUES(?,?,?,?,?,'applied',?,?)
            """.strip(),
            (
                audit_id,
                operation_id,
                operator,
                current_plan.source_user_id,
                current_plan.target_user_id,
                json.dumps(
                    {
                        "after_counts": after_counts,
                        "before_counts": before_counts,
                        "plan_fingerprint": current_plan.plan_fingerprint,
                        "access_expansion_count": len(current_plan.access_expansions),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                applied_at,
            ),
        )
        return AccountConsolidationResult(
            operation_id=operation_id,
            source_account_id=current_plan.source_account_id,
            target_account_id=current_plan.target_account_id,
            source_user_id=current_plan.source_user_id,
            target_user_id=current_plan.target_user_id,
            plan_fingerprint=current_plan.plan_fingerprint,
            applied_at=applied_at,
            idempotent_replay=False,
        )


__all__ = [
    "AccountConsolidationAccessExpansion",
    "AccountConsolidationConflict",
    "AccountConsolidationDependency",
    "AccountConsolidationPermissionDenied",
    "AccountConsolidationPlan",
    "AccountConsolidationResult",
    "AccountConsolidationStalePlan",
    "AccountConsolidationUnavailable",
    "apply_account_consolidation",
    "plan_account_consolidation",
]
