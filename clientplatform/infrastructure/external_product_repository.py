from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from clientplatform.domain.connections import normalize_credential_reference
from clientplatform.domain.external_products import (
    ExternalProductConnector,
    ExternalProductConnectorStatus,
    ExternalProductEvent,
    ExternalProductEventType,
    ExternalProductInvariantViolation,
    ExternalProductNotFound,
    ExternalProductReceipt,
    external_customer_fingerprint,
    normalize_external_product_key,
    normalize_external_product_name,
)
from clientplatform.domain.outcomes import (
    BusinessOutcomeEvent,
    OutcomeSource,
    OutcomeType,
)
from clientplatform.domain.tenancy import TenantContext, normalize_uuid
from clientplatform.infrastructure.attribution_repository import AttributionRepository
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.revenue_attribution_repository import (
    RevenueAttributionRepository,
)
from clientplatform.infrastructure.tenancy_repository import TenancyRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _value(row: Any, key: str, position: int) -> Any:
    return row[key] if hasattr(row, "keys") else row[position]


def _connector_from_row(row: Any) -> ExternalProductConnector:
    return ExternalProductConnector(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        product_key=str(_value(row, "product_key", 2)),
        display_name=str(_value(row, "display_name", 3)),
        webhook_secret_reference=str(_value(row, "webhook_secret_reference", 4)),
        status=ExternalProductConnectorStatus(str(_value(row, "status", 5))),
        created_by_member_id=str(_value(row, "created_by_member_id", 6)),
        created_at=str(_value(row, "created_at", 7)),
        updated_at=str(_value(row, "updated_at", 8)),
        activated_at=_optional(row, "activated_at", 9),
        disabled_at=_optional(row, "disabled_at", 10),
        revoked_at=_optional(row, "revoked_at", 11),
        last_event_at=_optional(row, "last_event_at", 12),
        last_error_at=_optional(row, "last_error_at", 13),
        last_error_code=_optional(row, "last_error_code", 14),
    )


def _optional(row: Any, key: str, position: int) -> str | None:
    value = _value(row, key, position)
    return None if value is None else str(value)


_CONNECTOR_SELECT = """
SELECT id,business_id,product_key,display_name,webhook_secret_reference,status,
       created_by_member_id,created_at,updated_at,activated_at,disabled_at,
       revoked_at,last_event_at,last_error_at,last_error_code
FROM external_product_connectors
""".strip()


class ExternalProductRepository:
    """Canonical tenant boundary for trusted external-product facts."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._tenancy = TenancyRepository(conn)

    def create_connector(
        self,
        *,
        actor: TenantContext,
        product_key: str,
        display_name: str,
        webhook_secret_reference: str,
        now: datetime | None = None,
    ) -> ExternalProductConnector:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        key = normalize_external_product_key(product_key)
        name = normalize_external_product_name(display_name)
        secret_ref = normalize_credential_reference(webhook_secret_reference)
        if not secret_ref.startswith("secret://env/CLIENTPLATFORM_SECRET_"):
            raise ExternalProductInvariantViolation(
                "external product webhook secret must use the ClientPlatform env namespace"
            )
        timestamp = _iso(now or _utc_now())
        connector_id = str(uuid4())
        self._conn.execute(
            """
            INSERT INTO external_product_connectors(
                id,business_id,product_key,display_name,webhook_secret_reference,
                status,created_by_member_id,created_at,updated_at,activated_at,
                disabled_at,revoked_at,last_event_at,last_error_at,last_error_code
            ) VALUES(?,?,?,?,?,'pending',?,?,?,NULL,NULL,NULL,NULL,NULL,NULL)
            ON CONFLICT(business_id,product_key) DO NOTHING
            """,
            (
                connector_id,
                current.business_id,
                key,
                name,
                secret_ref,
                current.membership_id,
                timestamp,
                timestamp,
            ),
        )
        row = self._conn.execute(
            _CONNECTOR_SELECT + " WHERE business_id=? AND product_key=? LIMIT 1",
            (current.business_id, key),
        ).fetchone()
        if row is None:
            raise RuntimeError("external product connector was not persisted")
        connector = _connector_from_row(row)
        if connector.webhook_secret_reference != secret_ref:
            raise ExternalProductInvariantViolation(
                "product_key already belongs to a connector with another secret reference"
            )
        return connector

    def get_connector(
        self,
        *,
        actor: TenantContext,
        connector_id: str,
    ) -> ExternalProductConnector:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        connector = self._get_connector_by_id(connector_id)
        current.assert_business(connector.business_id)
        return connector

    def get_active_for_ingress(self, *, connector_id: str) -> ExternalProductConnector:
        connector = self._get_connector_by_id(connector_id)
        if connector.status != ExternalProductConnectorStatus.ACTIVE:
            raise ExternalProductNotFound("external product connector is not active")
        return connector

    def activate_connector(
        self,
        *,
        actor: TenantContext,
        connector_id: str,
        now: datetime | None = None,
    ) -> ExternalProductConnector:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        connector = self.get_connector(actor=current, connector_id=connector_id)
        if connector.status == ExternalProductConnectorStatus.REVOKED:
            raise ExternalProductInvariantViolation("revoked connector cannot be activated")
        timestamp = _iso(now or _utc_now())
        self._conn.execute(
            """
            UPDATE external_product_connectors
            SET status='active',activated_at=COALESCE(activated_at,?),disabled_at=NULL,
                last_error_at=NULL,last_error_code=NULL,updated_at=?
            WHERE id=? AND business_id=? AND status!='revoked'
            """,
            (timestamp, timestamp, connector.id, current.business_id),
        )
        return self.get_connector(actor=current, connector_id=connector.id)

    def disable_connector(
        self,
        *,
        actor: TenantContext,
        connector_id: str,
        now: datetime | None = None,
    ) -> ExternalProductConnector:
        current = self._tenancy.resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        connector = self.get_connector(actor=current, connector_id=connector_id)
        if connector.status == ExternalProductConnectorStatus.REVOKED:
            raise ExternalProductInvariantViolation("revoked connector cannot be disabled")
        timestamp = _iso(now or _utc_now())
        self._conn.execute(
            """
            UPDATE external_product_connectors
            SET status='disabled',disabled_at=?,updated_at=?
            WHERE id=? AND business_id=? AND status!='revoked'
            """,
            (timestamp, timestamp, connector.id, current.business_id),
        )
        return self.get_connector(actor=current, connector_id=connector.id)

    def ingest_event(
        self,
        *,
        connector: ExternalProductConnector,
        event: ExternalProductEvent,
        payload_fingerprint: str,
        received_at: datetime | None = None,
    ) -> ExternalProductReceipt:
        active = self.get_active_for_ingress(connector_id=connector.id)
        if active.business_id != connector.business_id:
            raise ExternalProductInvariantViolation("connector business changed during ingress")
        fingerprint = str(payload_fingerprint or "").strip().lower()
        if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
            raise ValueError("payload_fingerprint must be a SHA-256 hex digest")
        existing = self._receipt_by_external_id(
            business_id=active.business_id,
            connector_id=active.id,
            external_event_id=event.external_event_id,
        )
        if existing is not None:
            if existing.payload_fingerprint != fingerprint:
                raise ExternalProductInvariantViolation(
                    "external event id was reused with a different payload"
                )
            return existing

        received = received_at or _utc_now()
        if received.tzinfo is None:
            raise ValueError("received_at must be timezone-aware")
        customer_fingerprint: str | None = None
        customer_id: str | None = None
        if event.customer_ref is not None:
            customer_fingerprint = external_customer_fingerprint(
                connector_id=active.id,
                customer_ref=event.customer_ref,
            )
            customer_id = self._ensure_customer(
                connector=active,
                customer_fingerprint=customer_fingerprint,
                now=received,
            )
        if event.acquisition is not None and customer_id is not None:
            AttributionRepository(self._conn).capture_external_product_touch(
                business_id=active.business_id,
                connector_id=active.id,
                source=event.acquisition.source,
                source_key=event.acquisition.source_key,
                customer_id=customer_id,
                occurred_at=event.occurred_at,
                metadata={"product_key": active.product_key},
            )

        outcome_event_id = self._append_outcome(
            connector=active,
            event=event,
            customer_id=customer_id,
            received_at=received,
        )
        receipt_id = str(uuid4())
        metadata_json = json.dumps(
            dict(event.metadata or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._conn.execute(
            """
            INSERT INTO external_product_event_receipts(
                id,business_id,connector_id,external_event_id,event_type,
                customer_id,customer_fingerprint,payload_fingerprint,outcome_event_id,
                occurred_at,received_at,metadata_json,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'accepted')
            ON CONFLICT(business_id,connector_id,external_event_id) DO NOTHING
            """,
            (
                receipt_id,
                active.business_id,
                active.id,
                event.external_event_id,
                event.event_type.value,
                customer_id,
                customer_fingerprint,
                fingerprint,
                outcome_event_id,
                _iso(event.occurred_at),
                _iso(received),
                metadata_json,
            ),
        )
        receipt = self._receipt_by_external_id(
            business_id=active.business_id,
            connector_id=active.id,
            external_event_id=event.external_event_id,
        )
        if receipt is None:
            raise RuntimeError("external product receipt was not persisted")
        if receipt.payload_fingerprint != fingerprint:
            raise ExternalProductInvariantViolation(
                "external event id was concurrently reused with another payload"
            )
        self._conn.execute(
            """
            UPDATE external_product_connectors
            SET last_event_at=?,last_error_at=NULL,last_error_code=NULL,updated_at=?
            WHERE id=? AND business_id=? AND status='active'
            """,
            (_iso(received), _iso(received), active.id, active.business_id),
        )
        return receipt

    def _append_outcome(
        self,
        *,
        connector: ExternalProductConnector,
        event: ExternalProductEvent,
        customer_id: str | None,
        received_at: datetime,
    ) -> str | None:
        outcome_type = {
            ExternalProductEventType.EVIDENCE: None,
            ExternalProductEventType.LEAD_CREATED: OutcomeType.LEAD_CREATED,
            ExternalProductEventType.LEAD_QUALIFIED: OutcomeType.LEAD_QUALIFIED,
            ExternalProductEventType.ORDER_PAID: OutcomeType.ORDER_PAID,
            ExternalProductEventType.REFUND_RECORDED: OutcomeType.REFUND_RECORDED,
        }[event.event_type]
        if outcome_type is None:
            return None
        if customer_id is None:
            raise ExternalProductInvariantViolation(
                "customer-linked outcome requires a resolved external customer"
            )
        metadata = {
            **dict(event.metadata or {}),
            "external_product_connector_id": connector.id,
            "external_product_key": connector.product_key,
            "external_event_type": event.event_type.value,
        }
        if event.acquisition is not None:
            metadata["acquisition_source"] = event.acquisition.source.value
            metadata["acquisition_source_key"] = event.acquisition.source_key
        if event.event_type == ExternalProductEventType.REFUND_RECORDED:
            related = self._receipt_by_external_id(
                business_id=connector.business_id,
                connector_id=connector.id,
                external_event_id=str(event.related_event_id),
            )
            if (
                related is None
                or related.event_type != ExternalProductEventType.ORDER_PAID
                or related.outcome_event_id is None
            ):
                raise ExternalProductInvariantViolation(
                    "refund related_event_id must reference an accepted order_paid event"
                )
            if related.customer_id != customer_id:
                raise ExternalProductInvariantViolation(
                    "refund customer must match the referenced order_paid event"
                )
            payment_row = self._conn.execute(
                """
                SELECT customer_id,currency
                FROM business_outcome_events
                WHERE business_id=? AND id=? AND outcome_type='order_paid'
                LIMIT 1
                """,
                (connector.business_id, related.outcome_event_id),
            ).fetchone()
            if payment_row is None:
                raise ExternalProductInvariantViolation(
                    "refund referenced payment outcome is unavailable"
                )
            payment_customer_id = _value(payment_row, "customer_id", 0)
            payment_currency = _value(payment_row, "currency", 1)
            if payment_customer_id is None or str(payment_customer_id) != customer_id:
                raise ExternalProductInvariantViolation(
                    "refund customer must match the referenced payment outcome"
                )
            if (
                event.money is None
                or payment_currency is None
                or str(payment_currency) != event.money.currency
            ):
                raise ExternalProductInvariantViolation(
                    "refund currency must match the referenced payment outcome"
                )
            metadata["payment_outcome_event_id"] = related.outcome_event_id

        outcome = OutcomeRepository(self._conn).append(
            BusinessOutcomeEvent(
                id=str(uuid4()),
                business_id=connector.business_id,
                outcome_type=outcome_type,
                occurred_at=event.occurred_at,
                source=OutcomeSource(
                    source_type="external_product",
                    source_id=f"{connector.product_key}:{event.external_event_id}",
                ),
                customer_id=customer_id,
                subject_ref=event.subject_ref,
                money=event.money,
                idempotency_key=(
                    f"external-product:{connector.id}:{event.external_event_id}"
                ),
                metadata=metadata,
                metadata_version=1,
                created_at=received_at,
            )
        )
        if outcome_type in {OutcomeType.ORDER_PAID, OutcomeType.REFUND_RECORDED}:
            RevenueAttributionRepository(self._conn).materialize_outcome(
                business_id=connector.business_id,
                outcome_event_id=outcome.id,
            )
        return outcome.id

    def _ensure_customer(
        self,
        *,
        connector: ExternalProductConnector,
        customer_fingerprint: str,
        now: datetime,
    ) -> str:
        identity_subject = f"extp:{connector.id}:{customer_fingerprint}"
        row = self._conn.execute(
            """
            SELECT ci.customer_id,c.status
            FROM customer_identities ci
            JOIN customers c ON c.id=ci.customer_id AND c.business_id=ci.business_id
            WHERE ci.business_id=? AND ci.platform='internal'
              AND ci.external_subject=? AND ci.status='active'
            LIMIT 1
            """,
            (connector.business_id, identity_subject),
        ).fetchone()
        if row is not None:
            if str(_value(row, "status", 1)) != "active":
                raise ExternalProductInvariantViolation(
                    "external product customer is archived"
                )
            return str(_value(row, "customer_id", 0))

        customer_id = str(
            uuid5(
                NAMESPACE_URL,
                f"clientplatform:{connector.business_id}:{identity_subject}:customer",
            )
        )
        identity_id = str(
            uuid5(
                NAMESPACE_URL,
                f"clientplatform:{connector.business_id}:{identity_subject}:identity",
            )
        )
        timestamp = _iso(now)
        self._conn.execute(
            """
            INSERT INTO customers(
                id,business_id,display_name,status,created_by_member_id,
                created_at,updated_at,archived_at,first_contact_at,last_contact_at
            ) VALUES(?, ?, NULL, 'active', ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                customer_id,
                connector.business_id,
                connector.created_by_member_id,
                timestamp,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        self._conn.execute(
            """
            INSERT INTO customer_identities(
                id,business_id,customer_id,platform,external_subject,
                username,display_name,status,created_at,updated_at,revoked_at,
                first_contact_at,last_contact_at
            ) VALUES(?,?,?,'internal',?,NULL,NULL,'active',?,?,NULL,?,?)
            ON CONFLICT(business_id,platform,external_subject) DO NOTHING
            """,
            (
                identity_id,
                connector.business_id,
                customer_id,
                identity_subject,
                timestamp,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        row = self._conn.execute(
            """
            SELECT ci.customer_id,c.status
            FROM customer_identities ci
            JOIN customers c ON c.id=ci.customer_id AND c.business_id=ci.business_id
            WHERE ci.business_id=? AND ci.platform='internal'
              AND ci.external_subject=? AND ci.status='active'
            LIMIT 1
            """,
            (connector.business_id, identity_subject),
        ).fetchone()
        if row is None:
            raise RuntimeError("external product customer identity was not persisted")
        if str(_value(row, "status", 1)) != "active":
            raise ExternalProductInvariantViolation("external product customer is archived")
        return str(_value(row, "customer_id", 0))

    def _receipt_by_external_id(
        self,
        *,
        business_id: str,
        connector_id: str,
        external_event_id: str,
    ) -> ExternalProductReceipt | None:
        row = self._conn.execute(
            """
            SELECT id,business_id,connector_id,external_event_id,event_type,
                   customer_id,customer_fingerprint,payload_fingerprint,
                   outcome_event_id,occurred_at,received_at
            FROM external_product_event_receipts
            WHERE business_id=? AND connector_id=? AND external_event_id=?
            LIMIT 1
            """,
            (business_id, connector_id, external_event_id),
        ).fetchone()
        if row is None:
            return None
        outcome_id = _value(row, "outcome_event_id", 8)
        return ExternalProductReceipt(
            id=str(_value(row, "id", 0)),
            business_id=str(_value(row, "business_id", 1)),
            connector_id=str(_value(row, "connector_id", 2)),
            external_event_id=str(_value(row, "external_event_id", 3)),
            event_type=ExternalProductEventType(str(_value(row, "event_type", 4))),
            customer_id=(
                None
                if _value(row, "customer_id", 5) is None
                else str(_value(row, "customer_id", 5))
            ),
            customer_fingerprint=(
                None
                if _value(row, "customer_fingerprint", 6) is None
                else str(_value(row, "customer_fingerprint", 6))
            ),
            payload_fingerprint=str(_value(row, "payload_fingerprint", 7)),
            outcome_event_id=None if outcome_id is None else str(outcome_id),
            occurred_at=str(_value(row, "occurred_at", 9)),
            received_at=str(_value(row, "received_at", 10)),
        )

    def _get_connector_by_id(self, connector_id: str) -> ExternalProductConnector:
        normalized = normalize_uuid(connector_id, field_name="connector_id")
        row = self._conn.execute(
            _CONNECTOR_SELECT + " WHERE id=? LIMIT 1",
            (normalized,),
        ).fetchone()
        if row is None:
            raise ExternalProductNotFound("external product connector was not found")
        return _connector_from_row(row)


__all__ = ["ExternalProductRepository"]
