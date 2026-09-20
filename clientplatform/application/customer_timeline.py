from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from clientplatform.domain.money import settlement_currency_minor_unit_exponent
from clientplatform.domain.outcomes import BusinessOutcomeEvent, OutcomeType
from clientplatform.domain.tenancy import TenantContext, TenantPermissionDenied
from clientplatform.infrastructure.attribution_repository import AttributionRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.outcome_repository import OutcomeRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import get_db_ro


class CustomerTimelineInvariantViolation(RuntimeError):
    """Stored canonical facts cannot be projected without inventing chronology."""


@dataclass(frozen=True, slots=True)
class CustomerTimelineEntry:
    kind: str
    occurred_at: datetime
    source_type: str
    source_id: str
    title: str
    detail: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    evidence_source: str | None = None
    evidence_quality: str | None = None
    observed_at: datetime | None = None
    evidence_revision: int | None = None
    evidence_state: str | None = None
    evidence_freshness: str | None = None
    fresh_until: datetime | None = None
    evidence_feedback: str | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("kind", "source_type", "source_id", "title"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.detail is not None:
            detail = str(self.detail).strip()
            object.__setattr__(self, "detail", detail or None)
        if self.amount_minor is not None:
            if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
                raise TypeError("amount_minor must be an integer")
            currency = str(self.currency or "").strip().upper()
            if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
                raise ValueError("monetary timeline entry requires a three-letter currency")
            object.__setattr__(self, "currency", currency)
        elif self.currency is not None:
            raise ValueError("currency without amount_minor is not allowed")
        for field_name in ("evidence_source", "evidence_quality"):
            value = getattr(self, field_name)
            if value is not None:
                normalized = " ".join(str(value).split()).strip()
                object.__setattr__(self, field_name, normalized or None)
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.fresh_until is not None and self.fresh_until.tzinfo is None:
            raise ValueError("fresh_until must be timezone-aware")
        if self.evidence_revision is not None and (
            isinstance(self.evidence_revision, bool)
            or not isinstance(self.evidence_revision, int)
            or self.evidence_revision < 1
        ):
            raise ValueError("evidence_revision must be a positive integer")
        limitations = tuple(
            text
            for item in tuple(self.limitations or ())
            if (text := _bounded_text(item, limit=200))
        )
        object.__setattr__(self, "limitations", limitations[:8])


@dataclass(frozen=True, slots=True)
class CustomerTimeline:
    business_id: str
    customer_id: str
    entries: tuple[CustomerTimelineEntry, ...]


_STAGE_LABELS = {
    "new": "Новое обращение",
    "contacted": "Связались с клиентом",
    "qualified": "Потребность подтверждена",
    "checkout": "Переход к оплате",
    "won": "Продажа завершена",
    "lost": "Обращение закрыто без продажи",
}

_OUTCOME_LABELS = {
    OutcomeType.LEAD_CREATED: "Появился лид",
    OutcomeType.LEAD_QUALIFIED: "Лид квалифицирован",
    OutcomeType.BOOKING_CREATED: "Создана запись",
    OutcomeType.BOOKING_CONFIRMED: "Запись подтверждена",
    OutcomeType.BOOKING_COMPLETED: "Запись завершена",
    OutcomeType.ORDER_PAID: "Получена оплата",
    OutcomeType.CUSTOMER_REACTIVATED: "Клиент вернулся",
    OutcomeType.REFUND_RECORDED: "Оформлен возврат",
    OutcomeType.OUTCOME_CORRECTION: "Результат скорректирован",
    OutcomeType.OUTCOME_REVERSAL: "Результат отменён",
}

_SOURCE_LABELS = {
    "organic": "Органический источник",
    "referral": "Рекомендация",
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "max": "MAX",
    "website": "Сайт",
    "yandex_direct": "Яндекс Директ",
    "partner": "Партнёр",
    "manual_import": "Добавлен вручную",
    "unknown": "Источник не определён",
}

_EXTERNAL_OBSERVATION_SCHEMA_V1 = "2026-09-20.v1"
_EXTERNAL_OBSERVATION_SCHEMA_V2 = "2026-09-20.v2"
_EXTERNAL_OBSERVATION_QUALITY_LABELS = {
    "source_asserted": "сообщено источником",
    "source_verified": "проверено источником",
    "derived": "вывод источника",
}

_VISIBLE_SALES_EVENTS = frozenset(
    {
        "stage_changed",
        "next_action_changed",
        "note_added",
        "followup_scheduled",
        "followup_opt_out",
    }
)


def _value(row: Any, key: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[key]
    return row[position]


def _parse_timestamp(value: object, *, field: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise CustomerTimelineInvariantViolation(f"{field} is missing")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CustomerTimelineInvariantViolation(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise CustomerTimelineInvariantViolation(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _payload(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _bounded_text(value: object, *, limit: int = 240) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _external_observation_entry(
    row: Any,
    *,
    now: datetime,
) -> CustomerTimelineEntry | None:
    metadata = _payload(_value(row, "metadata_json", 4))
    schema = metadata.get("external_observation_schema")
    if schema not in {_EXTERNAL_OBSERVATION_SCHEMA_V1, _EXTERNAL_OBSERVATION_SCHEMA_V2}:
        return None
    kind = str(metadata.get("external_observation_kind") or "").strip()
    label = _bounded_text(metadata.get("external_observation_label"), limit=160)
    provenance_ref = _bounded_text(
        metadata.get("external_observation_provenance_ref"),
        limit=300,
    )
    quality = str(metadata.get("external_observation_quality") or "").strip()
    quality_label = _EXTERNAL_OBSERVATION_QUALITY_LABELS.get(quality)
    if not kind or label is None or provenance_ref is None or quality_label is None:
        return None
    observed_at = _parse_timestamp(
        metadata.get("external_observation_observed_at"),
        field="external observation observed_at",
    )
    raw_limitations = metadata.get("external_observation_limitations") or []
    if not isinstance(raw_limitations, list):
        return None
    limitations = tuple(
        text
        for item in raw_limitations[:8]
        if isinstance(item, str) and (text := _bounded_text(item, limit=200))
    )
    connector_name = _bounded_text(_value(row, "connector_name", 5), limit=160)
    if connector_name is None:
        return None
    revision_value = _value(row, "observation_revision", 6)
    revision = None if revision_value is None else int(revision_value)
    state_value = _value(row, "observation_state", 7)
    state = None if state_value is None else str(state_value)
    fresh_value = _value(row, "observation_fresh_until", 8)
    fresh_until = (
        None
        if fresh_value is None
        else _parse_timestamp(fresh_value, field="external observation fresh_until")
    )
    feedback_value = _value(row, "observation_feedback", 9)
    feedback = None if feedback_value is None else str(feedback_value)
    if feedback not in {None, "useful", "incorrect", "wrong_customer"}:
        feedback = None
    if state == "retracted":
        freshness = "отозвано источником"
    elif fresh_until is None:
        freshness = "свежесть неизвестна"
    elif fresh_until < now:
        freshness = f"возможно устарело после {fresh_until.strftime('%d.%m.%Y %H:%M UTC')}"
    else:
        freshness = f"актуально до {fresh_until.strftime('%d.%m.%Y %H:%M UTC')}"
    detail_parts = [
        f"Источник: {connector_name}",
        quality_label,
        f"наблюдалось {observed_at.strftime('%d.%m.%Y %H:%M UTC')}",
        freshness,
    ]
    if revision is not None:
        detail_parts.append(f"ревизия {revision}")
    if limitations:
        detail_parts.append("ограничения: " + "; ".join(limitations))
    return CustomerTimelineEntry(
        kind=f"external_observation:{kind}",
        occurred_at=observed_at,
        source_type="external_product_receipt",
        source_id=str(_value(row, "id", 0)),
        title=("Внешнее наблюдение отозвано" if state == "retracted" else label),
        detail=" · ".join(detail_parts),
        evidence_source=connector_name,
        evidence_quality=quality_label,
        observed_at=observed_at,
        evidence_revision=revision,
        evidence_state=state,
        evidence_freshness=freshness,
        fresh_until=fresh_until,
        evidence_feedback=feedback,
        limitations=limitations,
    )


def _sales_event_entry(row: Any) -> CustomerTimelineEntry | None:
    event_type = str(_value(row, "event_type", 2) or "").strip()
    if event_type not in _VISIBLE_SALES_EVENTS:
        return None
    payload = _payload(_value(row, "payload_json", 3))
    lead_id = str(_value(row, "lead_id", 1))
    event_id = str(_value(row, "id", 0))
    occurred_at = _parse_timestamp(_value(row, "occurred_at", 4), field="sales occurred_at")
    title: str
    detail: str | None = None
    if event_type == "stage_changed":
        stage = str(payload.get("to_stage") or "").strip()
        title = _STAGE_LABELS.get(stage, "Этап обращения изменён")
    elif event_type == "next_action_changed":
        title = "Обновлён следующий шаг"
        detail = _bounded_text(payload.get("next_action"))
    elif event_type == "note_added":
        title = "Добавлена заметка"
        detail = _bounded_text(payload.get("note"))
    elif event_type == "followup_scheduled":
        title = "Запланирован следующий контакт"
        scheduled_at = payload.get("scheduled_at")
        if scheduled_at:
            detail = f"Запланировано на {scheduled_at}"
    else:
        title = "Клиент попросил не писать"
    return CustomerTimelineEntry(
        kind=f"sales:{event_type}",
        occurred_at=occurred_at,
        source_type="sales_event",
        source_id=event_id,
        title=title,
        detail=detail,
    )


def _direct_signed_outcome_money(
    event: BusinessOutcomeEvent,
) -> tuple[int, str] | None:
    if event.amount_minor is None or event.currency is None:
        return None
    if event.outcome_type == OutcomeType.ORDER_PAID:
        if event.amount_minor < 0:
            raise CustomerTimelineInvariantViolation(
                "order_paid outcome contains a negative amount"
            )
        return event.amount_minor, event.currency
    if event.outcome_type in {
        OutcomeType.REFUND_RECORDED,
        OutcomeType.OUTCOME_REVERSAL,
    }:
        return -abs(event.amount_minor), event.currency
    return event.amount_minor, event.currency


def _outcome_money(
    repository: OutcomeRepository,
    *,
    business_id: str,
    event: BusinessOutcomeEvent,
) -> tuple[int, str] | None:
    direct = _direct_signed_outcome_money(event)
    if direct is not None:
        return direct
    if (
        event.outcome_type != OutcomeType.OUTCOME_REVERSAL
        or event.source_type != "outcome_event"
    ):
        return None
    referenced = repository.get(
        business_id=business_id,
        event_id=event.source_id,
    )
    if referenced is None:
        return None
    referenced_money = _direct_signed_outcome_money(referenced)
    if referenced_money is None:
        return None
    amount_minor, currency = referenced_money
    return -amount_minor, currency


def _outcome_entry(
    repository: OutcomeRepository,
    *,
    business_id: str,
    event: BusinessOutcomeEvent,
) -> CustomerTimelineEntry:
    money = _outcome_money(repository, business_id=business_id, event=event)
    return CustomerTimelineEntry(
        kind=f"outcome:{event.outcome_type.value}",
        occurred_at=event.occurred_at.astimezone(timezone.utc),
        source_type="outcome_event",
        source_id=event.id,
        title=_OUTCOME_LABELS[event.outcome_type],
        amount_minor=None if money is None else money[0],
        currency=None if money is None else money[1],
    )


def _can_view_attribution(actor: TenantContext) -> bool:
    try:
        actor.assert_can_view_attribution_spine()
    except TenantPermissionDenied:
        return False
    return True


def _can_view_outcomes(actor: TenantContext) -> bool:
    try:
        actor.assert_can_view_outcome_ledger()
    except TenantPermissionDenied:
        return False
    return True


def get_customer_timeline(
    *,
    actor: TenantContext,
    customer_id: str,
    limit: int = 100,
    now: datetime | None = None,
) -> CustomerTimeline:
    """Project one customer chronology from existing canonical tenant facts.

    The projection is deliberately read-only. It does not materialize a second event
    store and it omits sensitive attribution/money facts for roles that cannot read
    those canonical ledgers.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 200:
        raise ValueError("limit must be an integer between 1 and 200")
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("timeline now must be timezone-aware")
    current_time = current_time.astimezone(timezone.utc)

    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_view_customer_records()
        record = CustomerRepository(conn).get_customer(actor=current, customer_id=customer_id)
        customer = record.customer

        entries: list[CustomerTimelineEntry] = [
            CustomerTimelineEntry(
                kind="customer:created",
                occurred_at=_parse_timestamp(customer.created_at, field="customer created_at"),
                source_type="customer",
                source_id=customer.id,
                title="Клиент добавлен",
            )
        ]

        if _can_view_attribution(current):
            trace = AttributionRepository(conn).get_customer_trace(
                business_id=current.business_id,
                customer_id=customer.id,
            )
            if trace is not None:
                entries.append(
                    CustomerTimelineEntry(
                        kind="acquisition:first_touch",
                        occurred_at=trace.touch.occurred_at.astimezone(timezone.utc),
                        source_type="acquisition_touch",
                        source_id=trace.touch.id,
                        title="Первый источник клиента",
                        detail=_SOURCE_LABELS.get(trace.touch.source.value, trace.touch.source.value),
                    )
                )

        observation_rows = conn.execute(
            """
            SELECT r.id,r.external_event_id,r.occurred_at,r.received_at,
                   r.metadata_json,c.display_name AS connector_name,
                   r.observation_revision,r.observation_state,r.observation_fresh_until,
                   f.feedback AS observation_feedback
            FROM external_product_event_receipts r
            JOIN external_product_connectors c
              ON c.id=r.connector_id AND c.business_id=r.business_id
            LEFT JOIN external_product_observation_feedback f
              ON f.business_id=r.business_id AND f.receipt_id=r.id
            WHERE r.business_id=? AND r.customer_id=?
              AND r.event_type='evidence' AND r.status='accepted'
              AND (
                r.observation_key IS NULL
                OR NOT EXISTS (
                    SELECT 1
                    FROM external_product_event_receipts newer
                    WHERE newer.business_id=r.business_id
                      AND newer.connector_id=r.connector_id
                      AND newer.customer_id=r.customer_id
                      AND newer.observation_key=r.observation_key
                      AND newer.status='accepted'
                      AND newer.observation_revision > r.observation_revision
                )
              )
            ORDER BY r.occurred_at,r.external_event_id
            """,
            (current.business_id, customer.id),
        ).fetchall()
        for row in observation_rows:
            entry = _external_observation_entry(row, now=current_time)
            if entry is not None:
                entries.append(entry)

        lead_rows = conn.execute(
            """
            SELECT id, source_kind, source_ref, created_at
            FROM clientplatform_sales_leads
            WHERE business_id=? AND customer_id=?
            ORDER BY created_at, id
            """,
            (current.business_id, customer.id),
        ).fetchall()
        for row in lead_rows:
            source_kind = str(_value(row, "source_kind", 1) or "").strip()
            entries.append(
                CustomerTimelineEntry(
                    kind="sales:lead_opened",
                    occurred_at=_parse_timestamp(_value(row, "created_at", 3), field="sales lead created_at"),
                    source_type="sales_lead",
                    source_id=str(_value(row, "id", 0)),
                    title="Появилось обращение",
                    detail=_SOURCE_LABELS.get(source_kind, None),
                )
            )

        sales_rows = conn.execute(
            """
            SELECT e.id, e.lead_id, e.event_type, e.payload_json, e.occurred_at
            FROM clientplatform_sales_events e
            JOIN clientplatform_sales_leads l
              ON l.id=e.lead_id AND l.business_id=e.business_id
            WHERE e.business_id=? AND l.customer_id=?
            ORDER BY e.occurred_at, e.id
            """,
            (current.business_id, customer.id),
        ).fetchall()
        for row in sales_rows:
            entry = _sales_event_entry(row)
            if entry is not None:
                entries.append(entry)

        if _can_view_outcomes(current):
            outcome_repository = OutcomeRepository(conn)
            outcomes = outcome_repository.list_events(
                business_id=current.business_id,
                customer_id=customer.id,
                limit=500,
            )
            lead_ids = {str(_value(row, "id", 0)) for row in lead_rows}
            for event in outcomes:
                if (
                    event.outcome_type == OutcomeType.LEAD_CREATED
                    and event.source_type == "sales_lead"
                    and event.source_id in lead_ids
                ):
                    continue
                entries.append(
                    _outcome_entry(
                        outcome_repository,
                        business_id=current.business_id,
                        event=event,
                    )
                )

    unique: dict[tuple[str, str, str], CustomerTimelineEntry] = {}
    for entry in entries:
        key = (entry.kind, entry.source_type, entry.source_id)
        unique.setdefault(key, entry)
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item.occurred_at,
            item.source_type,
            item.source_id,
            item.kind,
        ),
    )
    if len(ordered) > limit:
        ordered = ordered[-limit:]
    return CustomerTimeline(
        business_id=current.business_id,
        customer_id=customer.id,
        entries=tuple(ordered),
    )


def format_customer_timeline_lines(
    timeline: CustomerTimeline,
    *,
    max_entries: int = 8,
) -> tuple[str, ...]:
    """Render a compact channel-neutral owner chronology without provider jargon."""

    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
        raise ValueError("max_entries must be a positive integer")
    selected = timeline.entries[-max_entries:]
    lines: list[str] = []
    hidden = len(timeline.entries) - len(selected)
    if hidden > 0:
        lines.append(f"• Показаны последние {len(selected)} из {len(timeline.entries)} событий")
    for entry in selected:
        date_text = entry.occurred_at.astimezone(timezone.utc).strftime("%d.%m.%Y")
        line = f"• {date_text} · {entry.title}"
        if entry.detail:
            line += f" — {entry.detail}"
        if entry.amount_minor is not None and entry.currency is not None:
            exponent = settlement_currency_minor_unit_exponent(entry.currency)
            amount = Decimal(entry.amount_minor) / (Decimal(10) ** exponent)
            rendered = f"{amount:,.{exponent}f}" if exponent else f"{amount:,.0f}"
            rendered = rendered.replace(",", " ").replace(".", ",")
            line += f" · {rendered} {entry.currency}"
        lines.append(line)
    return tuple(lines)


__all__ = [
    "CustomerTimeline",
    "CustomerTimelineEntry",
    "CustomerTimelineInvariantViolation",
    "format_customer_timeline_lines",
    "get_customer_timeline",
]
