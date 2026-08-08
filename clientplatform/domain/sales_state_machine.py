from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from clientplatform.domain.sales import SalesLeadStage


class SalesConversationState(StrEnum):
    DISCOVERED = "discovered"
    ENGAGED = "engaged"
    NEED_KNOWN = "need_known"
    QUALIFIED = "qualified"
    OFFER_PRESENTED = "offer_presented"
    CHECKOUT = "checkout"
    WON = "won"
    LOST = "lost"
    HANDOFF = "handoff"


class SalesConversationEvent(StrEnum):
    INBOUND_RECEIVED = "inbound_received"
    CONTACT_RECORDED = "contact_recorded"
    NEED_CAPTURED = "need_captured"
    QUALIFICATION_PASSED = "qualification_passed"
    QUALIFICATION_FAILED = "qualification_failed"
    OFFER_PRESENTED = "offer_presented"
    CHECKOUT_STARTED = "checkout_started"
    PAYMENT_CONFIRMED = "payment_confirmed"
    DECLINED = "declined"
    HUMAN_REQUESTED = "human_requested"
    RISK_ESCALATED = "risk_escalated"
    HUMAN_RESUMED = "human_resumed"


@dataclass(frozen=True, slots=True)
class SalesTransition:
    previous: SalesConversationState
    event: SalesConversationEvent
    current: SalesConversationState


_TERMINAL = frozenset({SalesConversationState.WON, SalesConversationState.LOST})


def reduce_sales_conversation(
    state: SalesConversationState | str,
    event: SalesConversationEvent | str,
) -> SalesTransition:
    current = (
        state
        if isinstance(state, SalesConversationState)
        else SalesConversationState(str(state))
    )
    signal = (
        event
        if isinstance(event, SalesConversationEvent)
        else SalesConversationEvent(str(event))
    )

    # Handoff ownership is stored in the dedicated handoff queue. Request/resume
    # signals must never rewind funnel progress. The historical HANDOFF state is
    # retained only for compatibility with previously materialized projections.
    if signal in {
        SalesConversationEvent.HUMAN_REQUESTED,
        SalesConversationEvent.RISK_ESCALATED,
    }:
        return SalesTransition(current, signal, current)
    if signal == SalesConversationEvent.HUMAN_RESUMED:
        resumed = (
            SalesConversationState.ENGAGED
            if current == SalesConversationState.HANDOFF
            else current
        )
        return SalesTransition(current, signal, resumed)

    # Payment is hard evidence and may recover a previously declined opportunity.
    if signal == SalesConversationEvent.PAYMENT_CONFIRMED:
        return SalesTransition(current, signal, SalesConversationState.WON)
    if current == SalesConversationState.WON:
        return SalesTransition(current, signal, current)
    if signal in {
        SalesConversationEvent.DECLINED,
        SalesConversationEvent.QUALIFICATION_FAILED,
    }:
        return SalesTransition(current, signal, SalesConversationState.LOST)
    if current == SalesConversationState.LOST:
        return SalesTransition(current, signal, current)

    targets = {
        SalesConversationEvent.INBOUND_RECEIVED: SalesConversationState.ENGAGED,
        SalesConversationEvent.CONTACT_RECORDED: SalesConversationState.ENGAGED,
        SalesConversationEvent.NEED_CAPTURED: SalesConversationState.NEED_KNOWN,
        SalesConversationEvent.QUALIFICATION_PASSED: SalesConversationState.QUALIFIED,
        SalesConversationEvent.OFFER_PRESENTED: SalesConversationState.OFFER_PRESENTED,
        SalesConversationEvent.CHECKOUT_STARTED: SalesConversationState.CHECKOUT,
    }
    target = targets.get(signal)
    if target is None:
        raise ValueError(
            f"sales_conversation_transition_not_allowed:{current.value}:{signal.value}"
        )

    rank = {
        SalesConversationState.DISCOVERED: 0,
        SalesConversationState.ENGAGED: 1,
        SalesConversationState.NEED_KNOWN: 2,
        SalesConversationState.QUALIFIED: 3,
        SalesConversationState.OFFER_PRESENTED: 4,
        SalesConversationState.CHECKOUT: 5,
        SalesConversationState.WON: 6,
    }
    # Distinct provider events may repeat an already reached milestone. Treat
    # them as idempotent evidence instead of crashing a live conversation.
    if current in rank and rank[current] >= rank[target]:
        target = current
    elif current in rank and rank[target] != rank[current] + 1:
        raise ValueError(
            f"sales_conversation_transition_not_allowed:{current.value}:{signal.value}"
        )
    return SalesTransition(current, signal, target)


def coarse_sales_stage(
    state: SalesConversationState | str,
    *,
    previous_stage: SalesLeadStage | str = SalesLeadStage.NEW,
) -> SalesLeadStage:
    selected = (
        state
        if isinstance(state, SalesConversationState)
        else SalesConversationState(str(state))
    )
    previous = (
        previous_stage
        if isinstance(previous_stage, SalesLeadStage)
        else SalesLeadStage(str(previous_stage))
    )
    mapping = {
        SalesConversationState.DISCOVERED: SalesLeadStage.NEW,
        SalesConversationState.ENGAGED: SalesLeadStage.CONTACTED,
        SalesConversationState.NEED_KNOWN: SalesLeadStage.CONTACTED,
        SalesConversationState.QUALIFIED: SalesLeadStage.QUALIFIED,
        SalesConversationState.OFFER_PRESENTED: SalesLeadStage.QUALIFIED,
        SalesConversationState.CHECKOUT: SalesLeadStage.CHECKOUT,
        SalesConversationState.WON: SalesLeadStage.WON,
        SalesConversationState.LOST: SalesLeadStage.LOST,
    }
    # Handoff is an operational ownership state, not a regression in funnel stage.
    return previous if selected == SalesConversationState.HANDOFF else mapping[selected]


__all__ = [
    "SalesConversationEvent",
    "SalesConversationState",
    "SalesTransition",
    "coarse_sales_stage",
    "reduce_sales_conversation",
]
