from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

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


def _outcome_entry(event: BusinessOutcomeEvent) -> CustomerTimelineEntry:
    return CustomerTimelineEntry(
        kind=f"outcome:{event.outcome_type.value}",
        occurred_at=event.occurred_at.astimezone(timezone.utc),
        source_type=event.source_type,
        source_id=event.source_id,
        title=_OUTCOME_LABELS[event.outcome_type],
        amount_minor=event.amount_minor,
        currency=event.currency,
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
) -> CustomerTimeline:
    """Project one customer chronology from existing canonical tenant facts.

    The projection is deliberately read-only. It does not materialize a second event
    store and it omits sensitive attribution/money facts for roles that cannot read
    those canonical ledgers.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 200:
        raise ValueError("limit must be an integer between 1 and 200")

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
            outcomes = OutcomeRepository(conn).list_events(
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
                entries.append(_outcome_entry(event))

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
            amount = Decimal(entry.amount_minor) / Decimal(100)
            rendered = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
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
