from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from clientplatform.domain.tenancy import normalize_uuid


class SalesError(RuntimeError):
    """Base error for the tenant-scoped sales opportunity boundary."""


class SalesLeadNotFound(SalesError):
    """The requested sales lead does not exist in the active business."""


class SalesInvariantViolation(SalesError):
    """A sales transition or plan would violate a product invariant."""


class ContactBasis(StrEnum):
    INBOUND = "inbound"
    EXPLICIT_CONSENT = "explicit_consent"
    EXISTING_CUSTOMER = "existing_customer"
    REQUESTED_FOLLOWUP = "requested_followup"
    NONE = "none"


class SalesLeadStage(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    CHECKOUT = "checkout"
    WON = "won"
    LOST = "lost"


class SalesActionKind(StrEnum):
    RESPOND = "respond"
    ASK_QUALIFICATION = "ask_qualification"
    PRESENT_OFFER = "present_offer"
    CHECKOUT_FOLLOWUP = "checkout_followup"
    HUMAN_HANDOFF = "human_handoff"
    NOOP = "noop"


def _text(value: object, *, field_name: str, maximum: int) -> str:
    raw = str(value or "").replace("\x00", " ")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return normalized


def _optional_text(
    value: object | None,
    *,
    field_name: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    raw = str(value).replace("\x00", " ")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return normalized


def normalize_opportunity_key(value: object) -> str:
    return _text(value, field_name="opportunity_key", maximum=240)


def normalize_source_kind(value: object) -> str:
    normalized = _text(value, field_name="source_kind", maximum=64).lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", normalized):
        raise ValueError("source_kind must be a stable lowercase identifier")
    return normalized


def normalize_source_ref(value: object | None) -> str | None:
    return _optional_text(value, field_name="source_ref", maximum=240)


def normalize_next_action(value: object | None) -> str | None:
    return _optional_text(value, field_name="next_action", maximum=500)


def normalize_closure_reason(value: object | None) -> str | None:
    return _optional_text(value, field_name="closure_reason", maximum=500)


def normalize_due_at(value: object | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("due_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("due_at must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class SalesLead:
    id: str
    business_id: str
    opportunity_key: str
    customer_id: str
    offering_id: str | None
    source_kind: str
    source_ref: str | None
    contact_basis: ContactBasis
    stage: SalesLeadStage
    assigned_member_id: str | None
    last_signal_at: str
    created_at: str
    updated_at: str
    next_action: str | None = None
    due_at: str | None = None
    closure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_uuid(self.id, field_name="sales_lead_id"))
        object.__setattr__(
            self,
            "business_id",
            normalize_uuid(self.business_id, field_name="business_id"),
        )
        object.__setattr__(
            self,
            "customer_id",
            normalize_uuid(self.customer_id, field_name="customer_id"),
        )
        if self.offering_id is not None:
            object.__setattr__(
                self,
                "offering_id",
                normalize_uuid(self.offering_id, field_name="offering_id"),
            )
        if self.assigned_member_id is not None:
            object.__setattr__(
                self,
                "assigned_member_id",
                normalize_uuid(self.assigned_member_id, field_name="assigned_member_id"),
            )
        object.__setattr__(
            self,
            "opportunity_key",
            normalize_opportunity_key(self.opportunity_key),
        )
        object.__setattr__(self, "source_kind", normalize_source_kind(self.source_kind))
        object.__setattr__(self, "source_ref", normalize_source_ref(self.source_ref))
        object.__setattr__(self, "next_action", normalize_next_action(self.next_action))
        object.__setattr__(self, "due_at", normalize_due_at(self.due_at))
        object.__setattr__(
            self,
            "closure_reason",
            normalize_closure_reason(self.closure_reason),
        )
        object.__setattr__(
            self,
            "contact_basis",
            self.contact_basis
            if isinstance(self.contact_basis, ContactBasis)
            else ContactBasis(str(self.contact_basis)),
        )
        object.__setattr__(
            self,
            "stage",
            self.stage
            if isinstance(self.stage, SalesLeadStage)
            else SalesLeadStage(str(self.stage)),
        )
        if self.stage in {SalesLeadStage.WON, SalesLeadStage.LOST}:
            if self.next_action is not None or self.due_at is not None:
                raise SalesInvariantViolation("closed sales lead cannot keep a next action")
        elif self.closure_reason is not None:
            raise SalesInvariantViolation("open sales lead cannot keep a closure reason")
        if self.due_at is not None and self.next_action is None:
            raise SalesInvariantViolation("due_at requires a durable next action")


@dataclass(frozen=True, slots=True)
class SalesActionPlan:
    lead_id: str
    action_kind: SalesActionKind
    rationale: str
    requires_approval: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "lead_id",
            normalize_uuid(self.lead_id, field_name="sales_lead_id"),
        )
        action_kind = (
            self.action_kind
            if isinstance(self.action_kind, SalesActionKind)
            else SalesActionKind(str(self.action_kind).strip())
        )
        object.__setattr__(self, "action_kind", action_kind)
        object.__setattr__(
            self,
            "rationale",
            _text(self.rationale, field_name="rationale", maximum=500),
        )
        if not isinstance(self.requires_approval, bool):
            raise ValueError("requires_approval must be a boolean")


_CONTACTABLE_BASES = frozenset(
    {
        ContactBasis.INBOUND,
        ContactBasis.EXPLICIT_CONSENT,
        ContactBasis.EXISTING_CUSTOMER,
        ContactBasis.REQUESTED_FOLLOWUP,
    }
)


def can_contact(contact_basis: ContactBasis | str) -> bool:
    basis = (
        contact_basis
        if isinstance(contact_basis, ContactBasis)
        else ContactBasis(str(contact_basis))
    )
    return basis in _CONTACTABLE_BASES


def plan_sales_action(
    lead: SalesLead,
    *,
    model_confidence: float,
    unanswered_inbound: bool = False,
    explicit_human_request: bool = False,
    sensitive_context: bool = False,
) -> SalesActionPlan:
    """Create a proposal, never execute a message.

    The planner is deliberately fail-closed:
    - no cold first contact when a lawful/explicit contact basis is absent;
    - low-confidence, sensitive or explicitly human-requested conversations hand off;
    - all outward sales actions require approval at this layer. A later automation
      policy may relax that requirement only through the product's canonical controls.
    """

    for flag_name, flag_value in (
        ("unanswered_inbound", unanswered_inbound),
        ("explicit_human_request", explicit_human_request),
        ("sensitive_context", sensitive_context),
    ):
        if not isinstance(flag_value, bool):
            raise ValueError(f"{flag_name} must be a boolean")
    confidence = float(model_confidence)
    if not math.isfinite(confidence):
        raise ValueError("model_confidence must be finite")
    confidence = max(0.0, min(confidence, 1.0))
    if lead.stage in {SalesLeadStage.WON, SalesLeadStage.LOST}:
        return SalesActionPlan(
            lead_id=lead.id,
            action_kind=SalesActionKind.NOOP,
            rationale="lead_is_closed",
            requires_approval=False,
        )
    if explicit_human_request or sensitive_context or confidence < 0.72:
        return SalesActionPlan(
            lead_id=lead.id,
            action_kind=SalesActionKind.HUMAN_HANDOFF,
            rationale=(
                "explicit_human_request"
                if explicit_human_request
                else "sensitive_context"
                if sensitive_context
                else "low_model_confidence"
            ),
            requires_approval=False,
        )
    if not can_contact(lead.contact_basis):
        return SalesActionPlan(
            lead_id=lead.id,
            action_kind=SalesActionKind.NOOP,
            rationale="no_contact_basis",
            requires_approval=False,
        )
    if unanswered_inbound:
        kind = SalesActionKind.RESPOND
    elif lead.stage == SalesLeadStage.NEW:
        kind = (
            SalesActionKind.RESPOND
            if lead.contact_basis == ContactBasis.INBOUND
            else SalesActionKind.ASK_QUALIFICATION
        )
    elif lead.stage == SalesLeadStage.CONTACTED:
        kind = SalesActionKind.ASK_QUALIFICATION
    elif lead.stage == SalesLeadStage.QUALIFIED:
        kind = SalesActionKind.PRESENT_OFFER
    elif lead.stage == SalesLeadStage.CHECKOUT:
        kind = SalesActionKind.CHECKOUT_FOLLOWUP
    else:
        kind = SalesActionKind.NOOP
    return SalesActionPlan(
        lead_id=lead.id,
        action_kind=kind,
        rationale=f"stage:{lead.stage.value};basis:{lead.contact_basis.value}",
        requires_approval=kind
        not in {SalesActionKind.NOOP, SalesActionKind.HUMAN_HANDOFF},
    )


__all__ = [
    "ContactBasis",
    "SalesActionKind",
    "SalesActionPlan",
    "SalesError",
    "SalesInvariantViolation",
    "SalesLead",
    "SalesLeadNotFound",
    "SalesLeadStage",
    "can_contact",
    "normalize_closure_reason",
    "normalize_due_at",
    "normalize_next_action",
    "plan_sales_action",
]
