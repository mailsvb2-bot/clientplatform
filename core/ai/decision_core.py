from __future__ import annotations

import time
import uuid
import sqlite3
from typing import Any, Dict

from core.ai.decision_types import Decision, DecisionToken, WorldState
from core.runtime.sovereignty.enforcement import bind_signature, new_runtime_signature
from core.runtime.sovereignty.safe_mode import SAFE_MODE


ALLOWED_ENGINE_JOB_TYPES = frozenset({
    "demo_reminder",
    "demo_send",
    "funnel_offer",
    "after_paid_setup_ping",
    "funnel_nudge",
    "funnel_postdemo",
    "funnel_deadline",
    "funnel_lastcall",
    "sub_expiring_soon",
    "funnel2_demo_nopay_24h",
    "funnel2_expired_return_3d",
    "remind_continue",
    "post_prompt",
    "clientplatform_booking_reminder",
})


def allowed_engine_job_types() -> frozenset[str]:
    """Single exported policy surface for Engine/DecisionCore parity tests."""
    return ALLOWED_ENGINE_JOB_TYPES


class DecisionCore:
    """The only allowed decision authority."""

    _instance: "DecisionCore|None" = None

    def __new__(cls, *args: Any, **kwargs: Any):
        if cls._instance is not None:
            # Second init is an architectural violation by spec; handled by enforcement module
            raise RuntimeError("ARCH_VIOLATION: DecisionCore initialized twice")
        obj = super().__new__(cls)
        cls._instance = obj
        return obj

    def __init__(self) -> None:
        self.runtime_signature = new_runtime_signature()
        bind_signature(self.runtime_signature)

    @classmethod
    def instance(cls) -> "DecisionCore":
        if cls._instance is None:
            cls()
        assert cls._instance is not None
        return cls._instance

    def decide(self, world_state: WorldState) -> Decision:
        # Minimal baseline policy: if SAFE_MODE -> safe content; otherwise delegate to existing app via intent.
        decision_id = str(uuid.uuid4())
        now = time.time()
        ttl = int(world_state.get("token_ttl_sec") or 60)
        token = DecisionToken(decision_id=decision_id, issued_at=now, ttl_sec=ttl, signature=self.runtime_signature)

        # Observability: log decision creation (best-effort, must not break runtime)
        try:
            from services.events import log_runtime_event  # runtime layer
            user_id = int(world_state.get('user_id') or 0)
            if user_id:
                log_runtime_event(
                    user_id,
                    event_type='decision_made',
                    payload={'intent': world_state.get('intent'), 'meta': world_state.get('meta') or {}},
                    source=str(world_state.get('source') or 'telegram'),
                    correlation_id=token.nonce,
                    decision_id=decision_id,
                )
        except (sqlite3.Error, TypeError, ValueError):
            pass

        if SAFE_MODE.active:
            payload: Dict[str, Any] = {"type": "safe_content", "reason": SAFE_MODE.reason}
            return Decision(decision_id=decision_id, payload=payload, token=token, meta={"mode": "safe"})

        # Policy stub: payload can be filled by concrete product logic later.
        intent = world_state.get("intent")
        if intent == "engine_job_execute":
            job_type = str(world_state.get("job_type") or "").strip()
            if job_type not in allowed_engine_job_types():
                payload = {"type": "job_execution_denied", "reason": "unknown_job_type", "job_type": job_type}
                return Decision(
                    decision_id=decision_id,
                    payload=payload,
                    token=token,
                    meta={"mode": "engine", "policy": "engine_job_registry_v1"},
                )

            payload = {
                "type": "job_execution_allowed",
                "job_type": job_type,
                "user_id": int(world_state.get("user_id") or 0),
            }
            return Decision(
                decision_id=decision_id,
                payload=payload,
                token=token,
                meta={"mode": "engine", "policy": "engine_job_registry_v1"},
            )

        if intent == "admin_ai_prices":
            payload = {"type": "admin_ai_prices"}
            return Decision(decision_id=decision_id, payload=payload, token=token, meta={"mode": "admin"})

        payload = {"type": "noop", "intent": intent, "world": world_state}
        return Decision(decision_id=decision_id, payload=payload, token=token, meta={"mode": "baseline"})
