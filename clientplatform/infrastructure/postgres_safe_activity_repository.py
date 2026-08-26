from __future__ import annotations

"""Cross-database activity repository fixes and customer-role safety.

SQLite accepts integers directly in boolean expressions. PostgreSQL does not:
`0 OR status='active'` raises a datatype mismatch. Comparing the compatibility
flag with `1` keeps the existing integer parameters valid on both databases.
"""

from clientplatform.domain.activity import (
    ActivityInvariantViolation,
    BusinessCapability,
    BusinessOffering,
    InviteClaim,
    invite_token_hash,
)
from clientplatform.domain.customers import CustomerPlatform
from clientplatform.domain.tenancy import TenantContext, normalize_user_id
from clientplatform.infrastructure.activity_repository import (
    ActivityRepository as BaseActivityRepository,
    _capability_from_row,
    _offering_from_row,
)


class ActivityRepository(BaseActivityRepository):
    """Production-safe ActivityRepository for both SQLite and PostgreSQL."""

    def _assert_invite_claim_is_external(
        self,
        *,
        token: str,
        claiming_account_id: int | None = None,
        telegram_user_id: int | None = None,
    ) -> None:
        principal = (
            claiming_account_id
            if claiming_account_id is not None
            else telegram_user_id
        )
        if principal is None:
            return
        principal_id = normalize_user_id(principal)
        row = self._conn.execute(
            """
            SELECT 1
            FROM customer_invites ci
            JOIN business_members bm
              ON bm.business_id=ci.business_id
             AND bm.user_id=?
             AND bm.status='active'
            WHERE ci.token_hash=?
            LIMIT 1
            """,
            (principal_id, invite_token_hash(token)),
        ).fetchone()
        if row is not None:
            raise ActivityInvariantViolation(
                "Эту ссылку нельзя использовать владельцу или сотруднику "
                "собственного бизнеса. Отправьте её другому клиенту."
            )

    def claim_customer_invite(
        self,
        *,
        token: str,
        telegram_user_id: int,
        username: str | None,
        display_name: str | None,
        now: str | None = None,
    ) -> InviteClaim:
        principal_id = normalize_user_id(telegram_user_id)
        return self.claim_customer_invite_identity(
            token=token,
            platform=CustomerPlatform.TELEGRAM,
            external_subject=str(principal_id),
            username=username,
            display_name=display_name,
            claiming_account_id=principal_id,
            now=now,
        )

    def claim_customer_invite_identity(
        self,
        *,
        token: str,
        platform: CustomerPlatform | str,
        external_subject: str,
        username: str | None,
        display_name: str | None,
        claiming_account_id: int | None = None,
        expected_business_id: str | None = None,
        now: str | None = None,
    ) -> InviteClaim:
        self._assert_invite_claim_is_external(
            token=token,
            claiming_account_id=claiming_account_id,
        )
        return super().claim_customer_invite_identity(
            token=token,
            platform=platform,
            external_subject=external_subject,
            username=username,
            display_name=display_name,
            expected_business_id=expected_business_id,
            now=now,
        )

    def list_capabilities(
        self,
        *,
        actor: TenantContext,
        include_disabled: bool = False,
    ) -> list[BusinessCapability]:
        current = self._current_actor(actor)
        rows = self._conn.execute(
            """
            SELECT id, business_id, connector_key, kind, title, status,
                   created_by_member_id, created_at, updated_at
            FROM business_capabilities
            WHERE business_id=? AND (? = 1 OR status='active')
            ORDER BY created_at, connector_key
            """,
            (current.business_id, 1 if include_disabled else 0),
        ).fetchall()
        return [_capability_from_row(row) for row in rows]

    def list_offerings(
        self,
        *,
        actor: TenantContext,
        capability_id: str,
        include_archived: bool = False,
    ) -> list[BusinessOffering]:
        current = self._current_actor(actor)
        capability = self.get_capability(actor=current, capability_id=capability_id)
        rows = self._conn.execute(
            """
            SELECT id, business_id, capability_id, title, description, status,
                   created_by_member_id, created_at, updated_at
            FROM business_offerings
            WHERE business_id=? AND capability_id=?
              AND (? = 1 OR status='active')
            ORDER BY created_at, id
            """,
            (
                current.business_id,
                capability.id,
                1 if include_archived else 0,
            ),
        ).fetchall()
        return [_offering_from_row(row) for row in rows]


__all__ = ["ActivityRepository"]
