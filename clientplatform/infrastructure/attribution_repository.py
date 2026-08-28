from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from clientplatform.domain.attribution import (
    AcquisitionSource,
    AcquisitionTouch,
    AttributionIdentity,
    AttributionInvariantViolation,
    AttributionLink,
    AttributionModelVersion,
    AttributionTrace,
)
from clientplatform.domain.promotions import normalize_source_token


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("attribution timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _metadata_json(metadata: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(metadata),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _token_fingerprint(source_token: str) -> str:
    token = normalize_source_token(source_token)
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _promotion_source(*, channel: object, source_kind: object) -> AcquisitionSource:
    kind = str(source_kind or "").strip().lower()
    explicit = {
        "organic": AcquisitionSource.ORGANIC,
        "referral": AcquisitionSource.REFERRAL,
        "telegram": AcquisitionSource.TELEGRAM,
        "vk": AcquisitionSource.VK,
        "max": AcquisitionSource.MAX,
        "website": AcquisitionSource.WEBSITE,
        "yandex_direct": AcquisitionSource.YANDEX_DIRECT,
        "partner": AcquisitionSource.PARTNER,
        "manual_import": AcquisitionSource.MANUAL_IMPORT,
        "unknown": AcquisitionSource.UNKNOWN,
    }
    if kind in explicit:
        return explicit[kind]
    channel_value = str(getattr(channel, "value", channel) or "").strip().lower()
    return {
        "telegram": AcquisitionSource.TELEGRAM,
        "vk": AcquisitionSource.VK,
        "website": AcquisitionSource.WEBSITE,
    }.get(channel_value, AcquisitionSource.UNKNOWN)


def _identity_from_row(row: Any) -> AttributionIdentity:
    campaign_id = _value(row, "promotion_campaign_id", 7)
    return AttributionIdentity(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        source=AcquisitionSource(str(_value(row, "source", 2))),
        identity_kind=str(_value(row, "identity_kind", 3)),
        identity_fingerprint=str(_value(row, "identity_fingerprint", 4)),
        source_ref_type=str(_value(row, "source_ref_type", 5)),
        source_ref_id=str(_value(row, "source_ref_id", 6)),
        promotion_campaign_id=None if campaign_id is None else str(campaign_id),
        created_at=_parse_datetime(_value(row, "created_at", 8)),
    )


def _touch_from_row(row: Any) -> AcquisitionTouch:
    metadata = json.loads(str(_value(row, "metadata_json", 6)))
    if not isinstance(metadata, dict):
        raise ValueError("acquisition touch metadata must decode to a JSON object")
    return AcquisitionTouch(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        attribution_identity_id=str(_value(row, "attribution_identity_id", 2)),
        customer_id=str(_value(row, "customer_id", 3)),
        source=AcquisitionSource(str(_value(row, "source", 4))),
        occurred_at=_parse_datetime(_value(row, "occurred_at", 5)),
        metadata=metadata,
        metadata_version=int(_value(row, "metadata_version", 7)),
        created_at=_parse_datetime(_value(row, "created_at", 8)),
    )


def _link_from_row(row: Any) -> AttributionLink:
    customer_id = _value(row, "customer_id", 3)
    booking_slot_id = _value(row, "booking_slot_id", 4)
    return AttributionLink(
        id=str(_value(row, "id", 0)),
        business_id=str(_value(row, "business_id", 1)),
        touch_id=str(_value(row, "touch_id", 2)),
        customer_id=None if customer_id is None else str(customer_id),
        booking_slot_id=None if booking_slot_id is None else str(booking_slot_id),
        model_version=AttributionModelVersion(str(_value(row, "model_version", 5))),
        created_at=_parse_datetime(_value(row, "created_at", 6)),
    )


_IDENTITY_SELECT = """
    SELECT id, business_id, source, identity_kind, identity_fingerprint,
           source_ref_type, source_ref_id, promotion_campaign_id, created_at
    FROM attribution_identities
"""
_TOUCH_SELECT = """
    SELECT id, business_id, attribution_identity_id, customer_id, source,
           occurred_at, metadata_json, metadata_version, created_at
    FROM acquisition_touches
"""
_LINK_SELECT = """
    SELECT id, business_id, touch_id, customer_id, booking_slot_id,
           model_version, created_at
    FROM attribution_links
"""


class AttributionRepository:
    """Canonical business-scoped first-touch attribution over verified acquisition inputs."""

    def __init__(self, conn: Any):
        self._conn = conn

    def capture_promotion_touch(
        self,
        *,
        business_id: str,
        source_token: str,
        campaign_id: str,
        channel: object,
        source_kind: str,
        source_key: str,
        customer_id: str,
        occurred_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AttributionTrace:
        """Capture a verified promotion resolution without persisting its raw public token."""

        now = occurred_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        source = _promotion_source(channel=channel, source_kind=source_kind)
        normalized_kind = str(source_kind or "campaign").strip().lower() or "campaign"
        normalized_key = str(source_key or "").strip() or str(campaign_id)
        identity = self._ensure_identity(
            business_id=str(business_id),
            source=source,
            fingerprint=_token_fingerprint(source_token),
            source_ref_type=normalized_kind,
            source_ref_id=normalized_key,
            campaign_id=str(campaign_id),
            created_at=now,
        )
        touch_metadata = {
            "source_kind": normalized_kind,
            "source_key": normalized_key,
        }
        if metadata:
            touch_metadata.update(dict(metadata))
        touch = self._ensure_touch(
            business_id=str(business_id),
            identity=identity,
            customer_id=str(customer_id),
            occurred_at=now,
            metadata=touch_metadata,
        )
        self._ensure_customer_first_touch(
            business_id=str(business_id),
            customer_id=str(customer_id),
            touch_id=touch.id,
            created_at=now,
        )
        trace = self.get_customer_trace(
            business_id=str(business_id),
            customer_id=str(customer_id),
        )
        if trace is None:
            raise RuntimeError("customer attribution was not persisted")
        return trace

    def capture_external_product_touch(
        self,
        *,
        business_id: str,
        connector_id: str,
        source: AcquisitionSource | str,
        source_key: str,
        customer_id: str,
        occurred_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AttributionTrace:
        """Capture a trusted external-product acquisition without a raw public token."""

        now = occurred_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        selected_source = (
            source if isinstance(source, AcquisitionSource) else AcquisitionSource(str(source))
        )
        connector = str(connector_id or "").strip()
        key = " ".join(str(source_key or "").replace("\x00", " ").split())
        if not connector or len(connector) > 80:
            raise ValueError("external product connector id is invalid")
        if not key or len(key) > 200:
            raise ValueError("external product attribution source_key is invalid")
        source_ref_id = f"{connector}:{key}"
        fingerprint = hashlib.sha256(
            f"external_product\x00{business_id}\x00{connector}\x00{selected_source.value}\x00{key}".encode(
                "utf-8"
            )
        ).hexdigest()
        identity = self._ensure_external_product_identity(
            business_id=str(business_id),
            source=selected_source,
            fingerprint=fingerprint,
            source_ref_id=source_ref_id,
            created_at=now,
        )
        touch_metadata = {
            "external_product_connector_id": connector,
            "external_product_source_key": key,
        }
        if metadata:
            touch_metadata.update(dict(metadata))
        touch = self._ensure_touch(
            business_id=str(business_id),
            identity=identity,
            customer_id=str(customer_id),
            occurred_at=now,
            metadata=touch_metadata,
        )
        self._ensure_customer_first_touch(
            business_id=str(business_id),
            customer_id=str(customer_id),
            touch_id=touch.id,
            created_at=now,
        )
        trace = self.get_customer_trace(
            business_id=str(business_id),
            customer_id=str(customer_id),
        )
        if trace is None:
            raise RuntimeError("external product attribution was not persisted")
        return trace

    def link_booking_from_customer(
        self,
        *,
        business_id: str,
        customer_id: str,
        booking_slot_id: str,
        created_at: datetime | None = None,
    ) -> AttributionTrace | None:
        """Copy the customer's immutable first touch onto their booked slot."""

        now = created_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        customer_trace = self.get_customer_trace(
            business_id=str(business_id),
            customer_id=str(customer_id),
        )
        if customer_trace is None:
            return None
        booking_row = self._conn.execute(
            """
            SELECT booked_customer_id
            FROM booking_slots
            WHERE business_id=? AND id=?
            LIMIT 1
            """,
            (str(business_id), str(booking_slot_id)),
        ).fetchone()
        if booking_row is None:
            raise AttributionInvariantViolation("booking slot does not belong to this business")
        booked_customer_id = _value(booking_row, "booked_customer_id", 0)
        if booked_customer_id is None or str(booked_customer_id) != str(customer_id):
            raise AttributionInvariantViolation(
                "booking attribution customer does not match the booked slot"
            )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO attribution_links(
                id, business_id, touch_id, customer_id, booking_slot_id,
                model_version, created_at
            ) VALUES(?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                str(uuid4()),
                str(business_id),
                customer_trace.touch.id,
                str(booking_slot_id),
                AttributionModelVersion.FIRST_TOUCH_V1.value,
                _serialize_datetime(now),
            ),
        )
        trace = self.get_booking_trace(
            business_id=str(business_id),
            booking_slot_id=str(booking_slot_id),
        )
        if trace is None:
            raise RuntimeError("booking attribution was not persisted")
        if trace.touch.id != customer_trace.touch.id:
            raise AttributionInvariantViolation(
                "booking already belongs to a different first-touch attribution"
            )
        return trace

    def get_customer_trace(
        self,
        *,
        business_id: str,
        customer_id: str,
    ) -> AttributionTrace | None:
        link_row = self._conn.execute(
            _LINK_SELECT
            + " WHERE business_id=? AND customer_id=? AND model_version=? LIMIT 1",
            (
                str(business_id),
                str(customer_id),
                AttributionModelVersion.FIRST_TOUCH_V1.value,
            ),
        ).fetchone()
        return self._trace_from_link_row(link_row)

    def get_booking_trace(
        self,
        *,
        business_id: str,
        booking_slot_id: str,
    ) -> AttributionTrace | None:
        link_row = self._conn.execute(
            _LINK_SELECT
            + " WHERE business_id=? AND booking_slot_id=? AND model_version=? LIMIT 1",
            (
                str(business_id),
                str(booking_slot_id),
                AttributionModelVersion.FIRST_TOUCH_V1.value,
            ),
        ).fetchone()
        return self._trace_from_link_row(link_row)

    def _ensure_external_product_identity(
        self,
        *,
        business_id: str,
        source: AcquisitionSource,
        fingerprint: str,
        source_ref_id: str,
        created_at: datetime,
    ) -> AttributionIdentity:
        identity_kind = "external_product_source"
        self._conn.execute(
            """
            INSERT OR IGNORE INTO attribution_identities(
                id, business_id, source, identity_kind, identity_fingerprint,
                source_ref_type, source_ref_id, promotion_campaign_id, created_at
            ) VALUES(?, ?, ?, ?, ?, 'external_product', ?, NULL, ?)
            """,
            (
                str(uuid4()),
                business_id,
                source.value,
                identity_kind,
                fingerprint,
                source_ref_id,
                _serialize_datetime(created_at),
            ),
        )
        row = self._conn.execute(
            _IDENTITY_SELECT
            + " WHERE business_id=? AND identity_kind=? AND identity_fingerprint=? LIMIT 1",
            (business_id, identity_kind, fingerprint),
        ).fetchone()
        if row is None:
            raise RuntimeError("external product attribution identity was not persisted")
        identity = _identity_from_row(row)
        expected = (source, "external_product", source_ref_id, None)
        actual = (
            identity.source,
            identity.source_ref_type,
            identity.source_ref_id,
            identity.promotion_campaign_id,
        )
        if actual != expected:
            raise AttributionInvariantViolation(
                "external product attribution fingerprint has different semantics"
            )
        return identity

    def _ensure_identity(
        self,
        *,
        business_id: str,
        source: AcquisitionSource,
        fingerprint: str,
        source_ref_type: str,
        source_ref_id: str,
        campaign_id: str,
        created_at: datetime,
    ) -> AttributionIdentity:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO attribution_identities(
                id, business_id, source, identity_kind, identity_fingerprint,
                source_ref_type, source_ref_id, promotion_campaign_id, created_at
            ) VALUES(?, ?, ?, 'promotion_token', ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                business_id,
                source.value,
                fingerprint,
                source_ref_type,
                source_ref_id,
                campaign_id,
                _serialize_datetime(created_at),
            ),
        )
        row = self._conn.execute(
            _IDENTITY_SELECT
            + " WHERE business_id=? AND identity_kind='promotion_token' AND identity_fingerprint=? LIMIT 1",
            (business_id, fingerprint),
        ).fetchone()
        if row is None:
            raise RuntimeError("attribution identity was not persisted")
        identity = _identity_from_row(row)
        expected = (
            source,
            source_ref_type,
            source_ref_id,
            campaign_id,
        )
        actual = (
            identity.source,
            identity.source_ref_type,
            identity.source_ref_id,
            identity.promotion_campaign_id,
        )
        if actual != expected:
            raise AttributionInvariantViolation(
                "acquisition identity fingerprint already has different semantics"
            )
        return identity

    def _ensure_touch(
        self,
        *,
        business_id: str,
        identity: AttributionIdentity,
        customer_id: str,
        occurred_at: datetime,
        metadata: Mapping[str, Any],
    ) -> AcquisitionTouch:
        payload = _metadata_json(metadata)
        self._conn.execute(
            """
            INSERT OR IGNORE INTO acquisition_touches(
                id, business_id, attribution_identity_id, customer_id, source,
                occurred_at, metadata_json, metadata_version, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                str(uuid4()),
                business_id,
                identity.id,
                customer_id,
                identity.source.value,
                _serialize_datetime(occurred_at),
                payload,
                _serialize_datetime(occurred_at),
            ),
        )
        row = self._conn.execute(
            _TOUCH_SELECT
            + " WHERE business_id=? AND attribution_identity_id=? AND customer_id=? LIMIT 1",
            (business_id, identity.id, customer_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("acquisition touch was not persisted")
        touch = _touch_from_row(row)
        if touch.source != identity.source:
            raise AttributionInvariantViolation("acquisition touch source does not match its identity")
        return touch

    def _ensure_customer_first_touch(
        self,
        *,
        business_id: str,
        customer_id: str,
        touch_id: str,
        created_at: datetime,
    ) -> AttributionLink:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO attribution_links(
                id, business_id, touch_id, customer_id, booking_slot_id,
                model_version, created_at
            ) VALUES(?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                str(uuid4()),
                business_id,
                touch_id,
                customer_id,
                AttributionModelVersion.FIRST_TOUCH_V1.value,
                _serialize_datetime(created_at),
            ),
        )
        row = self._conn.execute(
            _LINK_SELECT
            + " WHERE business_id=? AND customer_id=? AND model_version=? LIMIT 1",
            (
                business_id,
                customer_id,
                AttributionModelVersion.FIRST_TOUCH_V1.value,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("customer attribution link was not persisted")
        return _link_from_row(row)

    def _trace_from_link_row(self, link_row: Any) -> AttributionTrace | None:
        if link_row is None:
            return None
        link = _link_from_row(link_row)
        touch_row = self._conn.execute(
            _TOUCH_SELECT + " WHERE business_id=? AND id=? LIMIT 1",
            (link.business_id, link.touch_id),
        ).fetchone()
        if touch_row is None:
            raise RuntimeError("attribution link references a missing touch")
        touch = _touch_from_row(touch_row)
        identity_row = self._conn.execute(
            _IDENTITY_SELECT + " WHERE business_id=? AND id=? LIMIT 1",
            (link.business_id, touch.attribution_identity_id),
        ).fetchone()
        if identity_row is None:
            raise RuntimeError("acquisition touch references a missing identity")
        identity = _identity_from_row(identity_row)
        return AttributionTrace(identity=identity, touch=touch, link=link)
