from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from clientplatform.domain.customers import normalize_identity_subject
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure import TenancyRepository
from config.settings import ADMIN_IDS
from services.admin import is_platform_admin
from services.db import get_db, get_db_ro


@dataclass(frozen=True, slots=True)
class CustomerActivityRow:
    customer_id: str
    business_id: str
    business_name: str | None
    display_name: str | None
    username: str | None
    platforms: tuple[str, ...]
    first_contact_at: str
    last_contact_at: str


@dataclass(frozen=True, slots=True)
class CustomerActivitySummary:
    total: int
    new_today: int
    new_7d: int
    active_today: int
    by_platform: dict[str, int]
    recent: tuple[CustomerActivityRow, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: Any) -> str:
    return str(value or "").strip()


def _parse_dt(value: Any) -> datetime | None:
    raw = _iso(value)
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _staff_subjects(conn: Any) -> dict[str, set[str]]:
    rows = conn.execute(
        """
        SELECT business_id, user_id
        FROM business_members
        WHERE status='active'
        """
    ).fetchall()
    out: dict[str, set[str]] = {}
    for row in rows:
        business_id = str(_row_value(row, "business_id", 0))
        out.setdefault(business_id, set()).add(str(_row_value(row, "user_id", 1)))
    return out


def _load_activity(
    conn: Any,
    *,
    business_id: str | None,
    exclude_platform_admins: bool,
    now: datetime,
    limit: int,
) -> CustomerActivitySummary:
    params: tuple[Any, ...]
    where = "WHERE c.status='active'"
    if business_id is not None:
        where += " AND c.business_id=?"
        params = (business_id,)
    else:
        params = ()
    customers = conn.execute(
        f"""
        SELECT c.id, c.business_id, c.display_name,
               COALESCE(c.first_contact_at, c.created_at) AS first_contact_at,
               COALESCE(c.last_contact_at, c.updated_at, c.created_at) AS last_contact_at,
               b.name AS business_name
        FROM customers c
        LEFT JOIN businesses b ON b.id=c.business_id
        {where}
        """,
        params,
    ).fetchall()
    identities = conn.execute(
        """
        SELECT business_id, customer_id, platform, external_subject, username,
               display_name,
               COALESCE(first_contact_at, created_at) AS first_contact_at,
               COALESCE(last_contact_at, updated_at, created_at) AS last_contact_at
        FROM customer_identities
        WHERE status='active'
        """
        + (" AND business_id=?" if business_id is not None else ""),
        params,
    ).fetchall()

    staff = _staff_subjects(conn)
    global_admin_subjects = {str(int(value)) for value in (ADMIN_IDS or [])}
    by_customer: dict[tuple[str, str], list[Any]] = {}
    for identity in identities:
        key = (
            str(_row_value(identity, "business_id", 0)),
            str(_row_value(identity, "customer_id", 1)),
        )
        by_customer.setdefault(key, []).append(identity)

    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    week_start = now - timedelta(days=7)
    rows: list[CustomerActivityRow] = []
    platform_customers: dict[str, set[tuple[str, str]]] = {}

    for customer in customers:
        bid = str(_row_value(customer, "business_id", 1))
        cid = str(_row_value(customer, "id", 0))
        customer_identities = by_customer.get((bid, cid), [])
        telegram_subjects = {
            str(_row_value(identity, "external_subject", 3))
            for identity in customer_identities
            if str(_row_value(identity, "platform", 2)).lower() == "telegram"
        }
        excluded = bool(telegram_subjects & staff.get(bid, set()))
        if exclude_platform_admins:
            excluded = excluded or bool(telegram_subjects & global_admin_subjects)
        if excluded:
            continue

        first_raw = _iso(_row_value(customer, "first_contact_at", 3))
        last_raw = _iso(_row_value(customer, "last_contact_at", 4))
        first_candidates = [first_raw]
        last_candidates = [last_raw]
        platforms: set[str] = set()
        username: str | None = None
        identity_name: str | None = None
        for identity in customer_identities:
            platform = str(_row_value(identity, "platform", 2)).strip().lower()
            if platform:
                platforms.add(platform)
                platform_customers.setdefault(platform, set()).add((bid, cid))
            candidate_username = _iso(_row_value(identity, "username", 4))
            if candidate_username and username is None:
                username = candidate_username
            candidate_name = _iso(_row_value(identity, "display_name", 5))
            if candidate_name and identity_name is None:
                identity_name = candidate_name
            first_candidates.append(_iso(_row_value(identity, "first_contact_at", 6)))
            last_candidates.append(_iso(_row_value(identity, "last_contact_at", 7)))

        first_values = [(parsed, raw) for raw in first_candidates if (parsed := _parse_dt(raw)) is not None]
        last_values = [(parsed, raw) for raw in last_candidates if (parsed := _parse_dt(raw)) is not None]
        first_contact_at = min(first_values, key=lambda pair: pair[0])[1] if first_values else first_raw
        last_contact_at = max(last_values, key=lambda pair: pair[0])[1] if last_values else last_raw
        display_name = _iso(_row_value(customer, "display_name", 2)) or identity_name or None
        business_name = _iso(_row_value(customer, "business_name", 5)) or None
        rows.append(
            CustomerActivityRow(
                customer_id=cid,
                business_id=bid,
                business_name=business_name,
                display_name=display_name,
                username=username,
                platforms=tuple(sorted(platforms)),
                first_contact_at=first_contact_at,
                last_contact_at=last_contact_at,
            )
        )

    def _after(value: str, boundary: datetime) -> bool:
        parsed = _parse_dt(value)
        return parsed is not None and parsed >= boundary

    rows.sort(key=lambda item: _parse_dt(item.last_contact_at) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return CustomerActivitySummary(
        total=len(rows),
        new_today=sum(1 for row in rows if _after(row.first_contact_at, today)),
        new_7d=sum(1 for row in rows if _after(row.first_contact_at, week_start)),
        active_today=sum(1 for row in rows if _after(row.last_contact_at, today)),
        by_platform={platform: len(keys) for platform, keys in sorted(platform_customers.items())},
        recent=tuple(rows[: max(1, min(int(limit), 100))]),
    )


def tenant_customer_activity(
    *,
    actor: TenantContext,
    now: datetime | None = None,
    limit: int = 25,
) -> CustomerActivitySummary:
    """Return only clients in actor's current business; caller cannot override that scope."""
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_customer_records()
        return _load_activity(
            conn,
            business_id=current.business_id,
            exclude_platform_admins=True,
            now=(now or _utc_now()).astimezone(timezone.utc),
            limit=limit,
        )


def platform_customer_activity(
    *,
    requester_user_id: int,
    now: datetime | None = None,
    limit: int = 25,
) -> CustomerActivitySummary:
    """Global projection reserved for high-trust ClientPlatform admins."""
    if not is_platform_admin(int(requester_user_id)):
        raise PermissionError("platform customer activity requires platform admin")
    with get_db_ro() as conn:
        return _load_activity(
            conn,
            business_id=None,
            exclude_platform_admins=True,
            now=(now or _utc_now()).astimezone(timezone.utc),
            limit=limit,
        )


def record_customer_contact(
    *,
    business_id: str,
    platform: str,
    external_subject: str,
    username: str | None = None,
    display_name: str | None = None,
    at: datetime | None = None,
) -> bool:
    """Touch one already-linked customer identity without creating shadow identities."""
    bid = normalize_uuid(business_id, field_name="business_id")
    normalized_platform, normalized_subject = normalize_identity_subject(platform, external_subject)
    timestamp = (at or _utc_now()).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT customer_id
            FROM customer_identities
            WHERE business_id=? AND platform=? AND external_subject=? AND status='active'
            LIMIT 1
            """,
            (bid, normalized_platform.value, normalized_subject),
        ).fetchone()
        if row is None:
            return False
        customer_id = str(_row_value(row, "customer_id", 0))
        conn.execute(
            """
            UPDATE customer_identities
            SET username=COALESCE(?, username),
                display_name=COALESCE(?, display_name),
                first_contact_at=COALESCE(first_contact_at, created_at, ?),
                last_contact_at=?, updated_at=?
            WHERE business_id=? AND customer_id=? AND platform=? AND external_subject=?
              AND status='active'
            """,
            (
                (username or "").strip() or None,
                (display_name or "").strip() or None,
                timestamp,
                timestamp,
                timestamp,
                bid,
                customer_id,
                normalized_platform.value,
                normalized_subject,
            ),
        )
        conn.execute(
            """
            UPDATE customers
            SET first_contact_at=COALESCE(first_contact_at, created_at, ?),
                last_contact_at=?, updated_at=?
            WHERE id=? AND business_id=? AND status='active'
            """,
            (timestamp, timestamp, timestamp, customer_id, bid),
        )
    return True
