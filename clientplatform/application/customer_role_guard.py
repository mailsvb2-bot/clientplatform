from __future__ import annotations

"""Prevent one Telegram principal from acting as staff and customer in one tenant."""

from typing import Any

from clientplatform.domain.tenancy import normalize_user_id, normalize_uuid


_SELF_CUSTOMER_MESSAGE = (
    "Собственный аккаунт владельца или сотрудника нельзя использовать как клиента "
    "этого же бизнеса. Для проверки откройте ссылку с другого Telegram-аккаунта."
)


def active_member_business_ids(conn: Any, *, telegram_user_id: int) -> set[str]:
    principal_id = normalize_user_id(telegram_user_id)
    rows = conn.execute(
        """
        SELECT business_id
        FROM business_members
        WHERE user_id=? AND status='active'
        """,
        (principal_id,),
    ).fetchall()
    return {str(row["business_id"] if hasattr(row, "keys") else row[0]) for row in rows}


def assert_external_customer(
    conn: Any,
    *,
    telegram_user_id: int,
    business_id: str,
) -> None:
    principal_id = normalize_user_id(telegram_user_id)
    normalized_business_id = normalize_uuid(business_id, field_name="business_id")
    row = conn.execute(
        """
        SELECT 1
        FROM business_members
        WHERE business_id=? AND user_id=? AND status='active'
        LIMIT 1
        """,
        (normalized_business_id, principal_id),
    ).fetchone()
    if row is not None:
        raise ValueError(_SELF_CUSTOMER_MESSAGE)


__all__ = [
    "active_member_business_ids",
    "assert_external_customer",
]
