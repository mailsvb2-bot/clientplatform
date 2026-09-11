from __future__ import annotations

import hashlib

from .domain import CommerceOperation, CommerceProposal, ProposalKind, UsageSnapshot


def _proposal_id(operation: CommerceOperation) -> str:
    raw = "\0".join(
        (
            operation.operation_id,
            operation.business_id,
            operation.member_id,
            operation.sku,
            str(operation.requested_units),
        )
    )
    return "cp_aic_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def build_proposal(
    operation: CommerceOperation,
    usage: UsageSnapshot,
) -> CommerceProposal:
    projected_total = usage.used + operation.requested_units
    requires_upgrade = projected_total > usage.allowance
    return CommerceProposal(
        proposal_id=_proposal_id(operation),
        operation_id=operation.operation_id,
        business_id=operation.business_id,
        sku=operation.sku,
        kind=ProposalKind.UPGRADE if requires_upgrade else ProposalKind.INCLUDED,
        reason=(
            "canonical allowance exceeded"
            if requires_upgrade
            else "covered by canonical allowance"
        ),
        used=usage.used,
        allowance=usage.allowance,
        requested_units=operation.requested_units,
        projected_total=projected_total,
    )
