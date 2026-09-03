from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sqlite3
from typing import Any

from core.time_utils import utc_now
from services.db import db, tx
from services.messenger.platforms import normalize_platform, parse_platform


def _iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


class AccountIdentityConflict(RuntimeError):
    """Raised when one external messenger identity already belongs to another account."""


class AccountIdentityMergeInvariantError(RuntimeError):
    """Raised when a merged-account alias is cyclic, broken, or otherwise unsafe."""


def _sqlite_base_connection(conn: Any) -> sqlite3.Connection | None:
    current = conn
    seen: set[int] = set()
    for _ in range(4):
        if isinstance(current, sqlite3.Connection):
            return current
        marker = id(current)
        if marker in seen:
            return None
        seen.add(marker)
        current = getattr(current, "_conn", None)
        if current is None:
            return None
    return None


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: int
    status: str
    identities: list[dict[str, Any]]


def _account_row_in_conn(conn: Any, identifier: int):
    value = int(identifier)
    try:
        row = conn.execute(
            """
            SELECT account_id, primary_user_id, status, merged_into_account_id,
                   created_at, updated_at, merged_at, merged_by_user_id, merge_reason
            FROM accounts
            WHERE account_id=?
            LIMIT 1
            """.strip(),
            (value,),
        ).fetchone()
        if row is not None:
            return row
        return conn.execute(
            """
            SELECT account_id, primary_user_id, status, merged_into_account_id,
                   created_at, updated_at, merged_at, merged_by_user_id, merge_reason
            FROM accounts
            WHERE primary_user_id=?
            LIMIT 1
            """.strip(),
            (value,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Some isolated repository tests intentionally create a narrow SQLite
        # schema without the account authority. Decide from the actual connection
        # object, not the process-wide DATABASE_URL: CI may expose PostgreSQL while
        # a contract test still uses an in-memory sqlite3.Connection. Only a truly
        # absent accounts table is tolerated; a present-but-incompatible account
        # schema remains fail-closed.
        sqlite_conn = _sqlite_base_connection(conn)
        if sqlite_conn is None:
            raise
        accounts_table = sqlite_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts' LIMIT 1"
        ).fetchone()
        if accounts_table is not None:
            raise
        return None

def _resolve_canonical_account_id_in_conn(conn: Any, account_or_user_id: int) -> int:
    requested = int(account_or_user_id)
    row = _account_row_in_conn(conn, requested)
    if row is None:
        return requested
    current = int(row["account_id"])
    current_row = row
    seen: set[int] = set()
    for _ in range(32):
        if current in seen:
            raise AccountIdentityMergeInvariantError("account merge alias cycle detected")
        seen.add(current)
        merged_into = current_row["merged_into_account_id"]
        if merged_into is None:
            return current
        current = int(merged_into)
        current_row = _account_row_in_conn(conn, current)
        if current_row is None:
            raise AccountIdentityMergeInvariantError("account merge alias target is missing")
    raise AccountIdentityMergeInvariantError("account merge alias depth exceeded")


def _resolve_canonical_user_id_in_conn(conn: Any, account_or_user_id: int) -> int:
    account_id = _resolve_canonical_account_id_in_conn(conn, int(account_or_user_id))
    row = _account_row_in_conn(conn, account_id)
    if row is None:
        return int(account_or_user_id)
    primary = row["primary_user_id"]
    return account_id if primary is None else int(primary)


def resolve_canonical_account_id(account_or_user_id: int) -> int:
    with db() as conn:
        return _resolve_canonical_account_id_in_conn(conn, int(account_or_user_id))


def resolve_canonical_user_id(account_or_user_id: int) -> int:
    with db() as conn:
        return _resolve_canonical_user_id_in_conn(conn, int(account_or_user_id))


def _ensure_account_in_conn(conn: Any, account_id: int, *, primary_user_id: int | None = None, status: str = "active") -> int:
    aid = int(account_id)
    primary = int(primary_user_id if primary_user_id is not None else aid)
    existing = _account_row_in_conn(conn, aid)
    if existing is not None:
        canonical = _resolve_canonical_account_id_in_conn(conn, aid)
        if canonical != aid:
            return canonical
        now = _iso_now()
        conn.execute(
            """
            UPDATE accounts
            SET primary_user_id=COALESCE(primary_user_id, ?), updated_at=?
            WHERE account_id=?
            """.strip(),
            (primary, now, aid),
        )
        return aid

    now = _iso_now()
    conn.execute(
        """
        INSERT INTO accounts(account_id, primary_user_id, status, created_at, updated_at)
        VALUES(?,?,?,?,?)
        """.strip(),
        (aid, primary, str(status or "active"), now, now),
    )
    return aid


def ensure_account(account_id: int, *, primary_user_id: int | None = None, status: str = "active") -> int:
    with db() as conn:
        with tx(conn):
            return _ensure_account_in_conn(
                conn,
                int(account_id),
                primary_user_id=primary_user_id,
                status=status,
            )


def _identity_row_in_conn(conn: Any, platform: str, external_user_id: str):
    return conn.execute(
        """
        SELECT account_id, platform, external_user_id, username, display_name, linked_at, last_seen_at
        FROM account_channel_identities
        WHERE platform=? AND external_user_id=?
        LIMIT 1
        """.strip(),
        (platform, external_user_id),
    ).fetchone()


def _identity_row(platform: str, external_user_id: str):
    with db() as conn:
        return _identity_row_in_conn(conn, platform, external_user_id)


def _platform_scoped_account_id(platform: str, external_user_id: str) -> int:
    """Return a stable platform-scoped id for a non-Telegram identity.

    Messenger user identifiers are only unique inside their own platform. They
    must never be reused as global account identifiers, otherwise a VK user and
    a MAX/Telegram user with the same numeric id can be silently merged. A high,
    platform-scoped digest keeps the legacy Telegram id namespace intact while
    making accidental cross-platform equality extremely unlikely.
    """

    raw = f"{platform}:{external_user_id}".encode("utf-8")
    value = int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big")
    # Telegram identifiers fit within 52 significant bits. Reserve the high
    # positive BIGINT range for internal accounts so existing code that requires
    # positive user ids remains valid without sharing Telegram's namespace.
    return (1 << 62) | (value & ((1 << 61) - 1))


def _proposed_account_id(
    platform: str,
    proposed_user_id: int | None,
    external_user_id: str | None,
) -> int:
    norm = parse_platform(platform)
    if norm is None:
        raise ValueError("invalid platform")
    raw = (external_user_id or "").strip()
    if norm == "telegram":
        if proposed_user_id is not None:
            return int(proposed_user_id)
        if raw.isdigit():
            return int(raw)
        raise ValueError("proposed_user_id is required for first-time Telegram identities")
    if not raw:
        raise ValueError("external_user_id is required for first-time non-Telegram identities")
    return _platform_scoped_account_id(norm, raw)


def _link_channel_to_account_in_conn(
    conn: Any,
    account_id: int,
    platform: str,
    external_user_id: str | None,
    *,
    username: str | None = None,
    display_name: str | None = None,
    verified: bool = False,
    link_source: str = "runtime",
    replace_existing: bool = False,
) -> int:
    """Link an identity using the caller's transaction.

    Bridge-token consumption uses this primitive so claiming the token and
    linking the external identity either commit together or both roll back.
    """

    norm = parse_platform(platform)
    if norm is None:
        raise ValueError("invalid platform")
    ext = (external_user_id or "").strip()
    aid = _ensure_account_in_conn(conn, int(account_id))
    now = _iso_now()
    verified_at = now if verified else None
    uname = (username or "").strip() or None
    dname = (display_name or "").strip() or None
    source = (link_source or "runtime").strip() or "runtime"

    if not ext:
        return aid

    existing = _identity_row_in_conn(conn, norm, ext)
    if existing is not None:
        existing_raw = int(existing["account_id"])
        existing_canonical = _resolve_canonical_account_id_in_conn(conn, existing_raw)
        if existing_canonical != aid:
            if not replace_existing:
                raise AccountIdentityConflict(
                    f"{norm}:{ext} already belongs to account_id={existing_raw}"
                )
            conn.execute(
                "DELETE FROM account_channel_identities WHERE platform=? AND external_user_id=?",
                (norm, ext),
            )
        elif existing_raw != aid:
            conn.execute(
                "UPDATE account_channel_identities SET account_id=? WHERE platform=? AND external_user_id=?",
                (aid, norm, ext),
            )
    conn.execute(
        """
        INSERT INTO account_channel_identities(
            account_id, platform, external_user_id, username, display_name,
            linked_at, last_seen_at, verified_at, link_source
        ) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, platform) DO UPDATE SET
            external_user_id=excluded.external_user_id,
            username=COALESCE(excluded.username, account_channel_identities.username),
            display_name=COALESCE(excluded.display_name, account_channel_identities.display_name),
            last_seen_at=excluded.last_seen_at,
            verified_at=COALESCE(account_channel_identities.verified_at, excluded.verified_at),
            link_source=excluded.link_source
        """.strip(),
        (aid, norm, ext, uname, dname, now, now, verified_at, source),
    )
    return aid


def link_channel_to_account(
    account_id: int,
    platform: str,
    external_user_id: str | None,
    *,
    username: str | None = None,
    display_name: str | None = None,
    verified: bool = False,
    link_source: str = "runtime",
    replace_existing: bool = False,
) -> int:
    """Link one platform identity to an account as a single atomic mutation.

    Replacing an existing owner used to delete the old identity in one transaction
    and create the new owner in another. A failure between those steps orphaned
    the identity. Conflict inspection, optional replacement, account creation and
    the final upsert now share one transaction, so any failure restores the
    original owner.
    """

    with db() as conn:
        with tx(conn):
            return _link_channel_to_account_in_conn(
                conn,
                int(account_id),
                platform,
                external_user_id,
                username=username,
                display_name=display_name,
                verified=verified,
                link_source=link_source,
                replace_existing=replace_existing,
            )


def resolve_account_for_identity(
    platform: str,
    external_user_id: str | None,
    *,
    proposed_user_id: int | None = None,
    username: str | None = None,
    display_name: str | None = None,
    allow_create: bool = True,
) -> int | None:
    norm = parse_platform(platform)
    if norm is None:
        raise ValueError("invalid platform")
    ext = (external_user_id or "").strip()
    if ext:
        existing = _identity_row(norm, ext)
        if existing is not None:
            aid = int(existing["account_id"])
            return link_channel_to_account(
                aid,
                norm,
                ext,
                username=username,
                display_name=display_name,
                link_source="seen_again",
            )
    if not allow_create:
        return None
    aid = _proposed_account_id(norm, proposed_user_id, ext)
    return link_channel_to_account(
        aid,
        norm,
        ext,
        username=username,
        display_name=display_name,
        link_source="first_seen",
    )


def get_account_snapshot(account_id: int) -> dict[str, Any]:
    aid = int(account_id)
    with db() as conn:
        account = _account_row_in_conn(conn, aid)
        canonical_account_id = _resolve_canonical_account_id_in_conn(conn, aid) if account else aid
        canonical_user_id = _resolve_canonical_user_id_in_conn(conn, aid) if account else aid
        identities = conn.execute(
            """
            SELECT platform, external_user_id, username, display_name, linked_at, last_seen_at, verified_at, link_source
            FROM account_channel_identities
            WHERE account_id=?
            ORDER BY last_seen_at DESC
            """.strip(),
            (aid,),
        ).fetchall()
    return {
        "account_id": aid,
        "primary_user_id": int(account["primary_user_id"]) if account and account["primary_user_id"] is not None else aid,
        "canonical_account_id": canonical_account_id,
        "canonical_user_id": canonical_user_id,
        "merged_into_account_id": None if not account or account["merged_into_account_id"] is None else int(account["merged_into_account_id"]),
        "status": normalize_platform(account["status"]) if account and str(account["status"] or "") in {"telegram", "vk", "max"} else (account["status"] if account else "missing"),
        "created_at": account["created_at"] if account else None,
        "updated_at": account["updated_at"] if account else None,
        "merged_at": account["merged_at"] if account else None,
        "merged_by_user_id": None if not account or account["merged_by_user_id"] is None else int(account["merged_by_user_id"]),
        "identities": [dict(row) for row in identities],
    }
