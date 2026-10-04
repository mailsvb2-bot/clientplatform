from __future__ import annotations

import hashlib
import json

from clientplatform.domain.tenancy import TenantContext
from clientplatform.domain.visual_scene_plan import VisualScenePlanReceipt
from clientplatform.domain.visual_style_intent import VisualStyleIntent
from clientplatform.infrastructure.visual_scene_plan_receipt_repository import (
    VisualScenePlanReceiptRepository,
)
from services.db import get_db, get_db_ro


VISUAL_SCENE_PLAN_KEY_VERSION = 1


def visual_scene_plan_key(
    *,
    request: str,
    style_intent: VisualStyleIntent,
) -> tuple[str, str, str]:
    owner_request = " ".join(
        str(request or "").replace("\x00", " ").split()
    ).strip()
    if not owner_request or len(owner_request) > 1500:
        raise ValueError("visual scene plan request is invalid")
    style_json = json.dumps(
        style_intent.normalized().to_mapping(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(
        (
            f"scene-plan-v{VISUAL_SCENE_PLAN_KEY_VERSION}\n"
            + owner_request
            + "\n"
            + style_json
        ).encode("utf-8")
    ).hexdigest()
    return digest, owner_request, style_json


def claim_visual_scene_plan(
    *,
    actor: TenantContext,
    request: str,
    style_intent: VisualStyleIntent,
) -> tuple[VisualScenePlanReceipt, bool]:
    plan_key, owner_request, style_json = visual_scene_plan_key(
        request=request,
        style_intent=style_intent,
    )
    with get_db() as conn:
        return VisualScenePlanReceiptRepository(conn).claim(
            actor=actor,
            plan_key=plan_key,
            request_text=owner_request,
            style_json=style_json,
        )


def get_visual_scene_plan(
    *,
    actor: TenantContext,
    request: str,
    style_intent: VisualStyleIntent,
) -> VisualScenePlanReceipt | None:
    plan_key, _request, _style = visual_scene_plan_key(
        request=request,
        style_intent=style_intent,
    )
    with get_db_ro() as conn:
        return VisualScenePlanReceiptRepository(conn).get_by_key(
            actor=actor,
            plan_key=plan_key,
        )


def complete_visual_scene_plan(
    *,
    actor: TenantContext,
    receipt_id: str,
    result_json: str,
) -> VisualScenePlanReceipt:
    with get_db() as conn:
        return VisualScenePlanReceiptRepository(conn).complete(
            actor=actor,
            receipt_id=receipt_id,
            result_json=result_json,
        )


def mark_visual_scene_plan_ambiguous(
    *,
    actor: TenantContext,
    receipt_id: str,
) -> VisualScenePlanReceipt:
    with get_db() as conn:
        return VisualScenePlanReceiptRepository(conn).mark_ambiguous(
            actor=actor,
            receipt_id=receipt_id,
        )


__all__ = [
    "VISUAL_SCENE_PLAN_KEY_VERSION",
    "claim_visual_scene_plan",
    "complete_visual_scene_plan",
    "get_visual_scene_plan",
    "mark_visual_scene_plan_ambiguous",
    "visual_scene_plan_key",
]
