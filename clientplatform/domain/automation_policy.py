from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clientplatform.domain.tenancy import normalize_uuid

_TOKEN_RE = re.compile(r"^[a-z][a-z0-9._:-]{0,79}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class AutomationPolicyError(RuntimeError):
    """Base error for canonical automation-policy operations."""


class AutomationPolicyInvariantViolation(AutomationPolicyError):
    """A policy or policy check violates a fail-closed invariant."""


class AutomationPolicyNotFound(AutomationPolicyError):
    """No policy in the active business matched the request."""


class AutomationPolicyConflict(AutomationPolicyError):
    """The policy changed between review and mutation."""


class AutomationPolicyStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class AutomationMode(StrEnum):
    CAUTIOUS = "cautious"
    NORMAL = "normal"
    AUTOPILOT = "autopilot"


class PolicyDecision(StrEnum):
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"
    ALLOW = "allow"


@dataclass(frozen=True, slots=True)
class AutomationActionSemantics:
    action: str
    external_write: bool
    money_bearing: bool = False


_ACTION_SEMANTICS = {
    "growth.read_only_analysis": AutomationActionSemantics(
        action="growth.read_only_analysis",
        external_write=False,
    ),
    "ads.adjust_budget": AutomationActionSemantics(
        action="ads.adjust_budget",
        external_write=True,
        money_bearing=True,
    ),
    "sales.followup": AutomationActionSemantics(
        action="sales.followup",
        external_write=True,
    ),
    "payments.refund": AutomationActionSemantics(
        action="payments.refund",
        external_write=True,
        money_bearing=True,
    ),
}


def automation_action_semantics(action: object) -> AutomationActionSemantics | None:
    return _ACTION_SEMANTICS.get(_token(action, "action"))


def _token(value: object, name: str) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    if not _TOKEN_RE.fullmatch(normalized):
        raise ValueError(f"{name} is invalid")
    return normalized


def _tokens(values: tuple[str, ...] | list[str], name: str) -> tuple[str, ...]:
    normalized = tuple(sorted({_token(value, name) for value in values}))
    if len(normalized) > 128:
        raise ValueError(f"{name} has too many values")
    return normalized


def _currency(value: object, name: str = "currency") -> str:
    normalized = str(value or "").strip().upper()
    if not _CURRENCY_RE.fullmatch(normalized):
        raise ValueError(f"{name} must be a three-letter ISO currency code")
    return normalized


def _minor(value: object, name: str, *, zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must use integer minor units")
    try:
        normalized = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must use integer minor units") from exc
    if normalized < (0 if zero else 1) or normalized > 9_000_000_000_000_000:
        raise ValueError(f"{name} is outside the supported range")
    return normalized


def _timestamp(value: datetime | str, name: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _clock(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    try:
        parsed = time.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must use HH:MM") from exc
    if parsed.second or parsed.microsecond or len(raw) != 5:
        raise ValueError(f"{name} must use HH:MM")
    return parsed.strftime("%H:%M")


def _clock_contains(current: time, start: str, end: str) -> bool:
    left = time.fromisoformat(start)
    right = time.fromisoformat(end)
    if left < right:
        return left <= current < right
    return current >= left or current < right


def _stable_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class AutomationMoneyLimit:
    action: str
    currency: str
    max_per_action_minor: int
    max_daily_minor: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", _token(self.action, "money_limit.action"))
        object.__setattr__(self, "currency", _currency(self.currency, "money_limit.currency"))
        object.__setattr__(
            self,
            "max_per_action_minor",
            _minor(self.max_per_action_minor, "money_limit.max_per_action_minor"),
        )
        if self.max_daily_minor is not None:
            daily = _minor(self.max_daily_minor, "money_limit.max_daily_minor")
            if daily < self.max_per_action_minor:
                raise ValueError("money_limit.max_daily_minor must cover one allowed action")
            object.__setattr__(self, "max_daily_minor", daily)

    def payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "currency": self.currency,
            "max_per_action_minor": self.max_per_action_minor,
            "max_daily_minor": self.max_daily_minor,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AutomationMoneyLimit:
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class AutomationApprovalThreshold:
    action: str
    amount_minor: int | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", _token(self.action, "approval_threshold.action"))
        if self.amount_minor is None:
            if self.currency is not None:
                raise ValueError("approval threshold currency requires amount_minor")
            return
        object.__setattr__(
            self,
            "amount_minor",
            _minor(self.amount_minor, "approval_threshold.amount_minor", zero=True),
        )
        object.__setattr__(self, "currency", _currency(self.currency, "approval_threshold.currency"))

    def payload(self) -> dict[str, Any]:
        return {"action": self.action, "amount_minor": self.amount_minor, "currency": self.currency}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AutomationApprovalThreshold:
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class AutomationSchedule:
    timezone_name: str
    allowed_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    active_start: str | None = None
    active_end: str | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None

    def __post_init__(self) -> None:
        timezone_name = str(self.timezone_name or "").strip()
        try:
            ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("automation schedule timezone is invalid") from exc
        object.__setattr__(self, "timezone_name", timezone_name)
        weekdays = tuple(sorted({int(day) for day in self.allowed_weekdays}))
        if not weekdays or any(day < 0 or day > 6 for day in weekdays):
            raise ValueError("allowed_weekdays must contain values from 0 to 6")
        object.__setattr__(self, "allowed_weekdays", weekdays)
        for start_name, end_name in (("active_start", "active_end"), ("quiet_start", "quiet_end")):
            start = _clock(getattr(self, start_name), start_name)
            end = _clock(getattr(self, end_name), end_name)
            if (start is None) != (end is None):
                raise ValueError(f"{start_name} and {end_name} must be configured together")
            if start is not None and start == end:
                raise ValueError(f"{start_name} and {end_name} must not be equal")
            object.__setattr__(self, start_name, start)
            object.__setattr__(self, end_name, end)

    def permits(self, value: datetime | str) -> bool:
        instant = datetime.fromisoformat(_timestamp(value, "scheduled_at"))
        local = instant.astimezone(ZoneInfo(self.timezone_name))
        if local.weekday() not in self.allowed_weekdays:
            return False
        current = local.timetz().replace(tzinfo=None)
        if self.active_start is not None and self.active_end is not None:
            if not _clock_contains(current, self.active_start, self.active_end):
                return False
        if self.quiet_start is not None and self.quiet_end is not None:
            if _clock_contains(current, self.quiet_start, self.quiet_end):
                return False
        return True

    def payload(self) -> dict[str, Any]:
        return {
            "timezone_name": self.timezone_name,
            "allowed_weekdays": list(self.allowed_weekdays),
            "active_start": self.active_start,
            "active_end": self.active_end,
            "quiet_start": self.quiet_start,
            "quiet_end": self.quiet_end,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AutomationSchedule:
        values = dict(payload)
        values["allowed_weekdays"] = tuple(values.get("allowed_weekdays") or ())
        return cls(**values)


@dataclass(frozen=True, slots=True)
class AutomationPolicySpec:
    mode: AutomationMode
    allowed_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    allowed_channels: tuple[str, ...]
    allowed_audiences: tuple[str, ...]
    schedule: AutomationSchedule
    expires_at: str
    money_limits: tuple[AutomationMoneyLimit, ...] = ()
    ai_usage_limit_minor: int | None = None
    ai_usage_currency: str | None = None
    approval_required_actions: tuple[str, ...] = ()
    approval_required_channels: tuple[str, ...] = ()
    approval_thresholds: tuple[AutomationApprovalThreshold, ...] = ()
    allowed_content_topics: tuple[str, ...] = ()
    forbidden_claims: tuple[str, ...] = ()
    stop_conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", AutomationMode(self.mode))
        for name in (
            "allowed_actions",
            "forbidden_actions",
            "allowed_channels",
            "allowed_audiences",
            "approval_required_actions",
            "approval_required_channels",
            "allowed_content_topics",
            "forbidden_claims",
            "stop_conditions",
        ):
            object.__setattr__(self, name, _tokens(list(getattr(self, name)), name))
        overlap = set(self.allowed_actions) & set(self.forbidden_actions)
        if overlap:
            raise ValueError("an automation action cannot be both allowed and forbidden")
        limits = tuple(sorted(self.money_limits, key=lambda item: (item.action, item.currency)))
        if len({(item.action, item.currency) for item in limits}) != len(limits):
            raise ValueError("duplicate money limits are not allowed")
        object.__setattr__(self, "money_limits", limits)
        thresholds = tuple(
            sorted(
                self.approval_thresholds,
                key=lambda item: (item.action, item.currency or "", item.amount_minor or -1),
            )
        )
        object.__setattr__(self, "approval_thresholds", thresholds)
        object.__setattr__(self, "expires_at", _timestamp(self.expires_at, "expires_at"))
        if self.ai_usage_limit_minor is None:
            if self.ai_usage_currency is not None:
                raise ValueError("ai_usage_currency requires ai_usage_limit_minor")
        else:
            object.__setattr__(
                self,
                "ai_usage_limit_minor",
                _minor(self.ai_usage_limit_minor, "ai_usage_limit_minor", zero=True),
            )
            object.__setattr__(self, "ai_usage_currency", _currency(self.ai_usage_currency, "ai_usage_currency"))

    def payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "allowed_actions": list(self.allowed_actions),
            "forbidden_actions": list(self.forbidden_actions),
            "allowed_channels": list(self.allowed_channels),
            "allowed_audiences": list(self.allowed_audiences),
            "schedule": self.schedule.payload(),
            "expires_at": self.expires_at,
            "money_limits": [item.payload() for item in self.money_limits],
            "ai_usage_limit_minor": self.ai_usage_limit_minor,
            "ai_usage_currency": self.ai_usage_currency,
            "approval_required_actions": list(self.approval_required_actions),
            "approval_required_channels": list(self.approval_required_channels),
            "approval_thresholds": [item.payload() for item in self.approval_thresholds],
            "allowed_content_topics": list(self.allowed_content_topics),
            "forbidden_claims": list(self.forbidden_claims),
            "stop_conditions": list(self.stop_conditions),
        }

    @property
    def policy_hash(self) -> str:
        return hashlib.sha256(_stable_json(self.payload()).encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        return _stable_json(self.payload())

    @classmethod
    def from_json(cls, value: str) -> AutomationPolicySpec:
        try:
            payload = json.loads(str(value))
        except json.JSONDecodeError as exc:
            raise ValueError("automation policy JSON is invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("automation policy JSON must be an object")
        data = dict(payload)
        data["mode"] = AutomationMode(str(data.get("mode") or ""))
        for key in (
            "allowed_actions",
            "forbidden_actions",
            "allowed_channels",
            "allowed_audiences",
            "approval_required_actions",
            "approval_required_channels",
            "allowed_content_topics",
            "forbidden_claims",
            "stop_conditions",
        ):
            data[key] = tuple(data.get(key) or ())
        data["schedule"] = AutomationSchedule.from_payload(dict(data.get("schedule") or {}))
        data["money_limits"] = tuple(
            AutomationMoneyLimit.from_payload(dict(item)) for item in data.get("money_limits") or ()
        )
        data["approval_thresholds"] = tuple(
            AutomationApprovalThreshold.from_payload(dict(item)) for item in data.get("approval_thresholds") or ()
        )
        return cls(**data)


@dataclass(frozen=True, slots=True)
class AutomationPolicy:
    id: str
    business_id: str
    version: int
    status: AutomationPolicyStatus
    spec: AutomationPolicySpec
    policy_hash: str
    created_by_member_id: str
    created_at: str
    updated_at: str
    approved_by_member_id: str | None = None
    approved_at: str | None = None
    revoked_at: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "business_id", "created_by_member_id"):
            object.__setattr__(self, name, normalize_uuid(getattr(self, name), field_name=name))
        if self.approved_by_member_id is not None:
            object.__setattr__(
                self,
                "approved_by_member_id",
                normalize_uuid(self.approved_by_member_id, field_name="approved_by_member_id"),
            )
        if isinstance(self.version, bool) or int(self.version) < 1:
            raise ValueError("automation policy version must be positive")
        object.__setattr__(self, "version", int(self.version))
        object.__setattr__(self, "status", AutomationPolicyStatus(self.status))
        if not _HASH_RE.fullmatch(str(self.policy_hash)) or self.policy_hash != self.spec.policy_hash:
            raise AutomationPolicyInvariantViolation("automation policy hash does not match policy contents")
        object.__setattr__(self, "created_at", _timestamp(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _timestamp(self.updated_at, "updated_at"))
        if self.approved_at is not None:
            object.__setattr__(self, "approved_at", _timestamp(self.approved_at, "approved_at"))
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", _timestamp(self.revoked_at, "revoked_at"))
        if self.status == AutomationPolicyStatus.APPROVED:
            if self.approved_by_member_id is None or self.approved_at is None:
                raise AutomationPolicyInvariantViolation("approved policy requires owner approval evidence")

    def is_effective(self, *, now: datetime | str) -> bool:
        if self.status != AutomationPolicyStatus.APPROVED:
            return False
        return datetime.fromisoformat(_timestamp(now, "now")) < datetime.fromisoformat(self.spec.expires_at)


@dataclass(frozen=True, slots=True)
class AutomationCandidateAction:
    business_id: str
    action: str
    external_write: bool
    channel: str | None = None
    audience: str | None = None
    scheduled_at: datetime | str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    projected_daily_amount_minor: int | None = None
    ai_usage_minor: int | None = None
    ai_usage_currency: str | None = None
    content_topics: tuple[str, ...] = ()
    claims: tuple[str, ...] = ()
    active_stop_conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "business_id", normalize_uuid(self.business_id, field_name="business_id"))
        object.__setattr__(self, "action", _token(self.action, "action"))
        if not isinstance(self.external_write, bool):
            raise ValueError("external_write must be boolean")
        for name in ("channel", "audience"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _token(value, name))
        if self.scheduled_at is not None:
            object.__setattr__(self, "scheduled_at", _timestamp(self.scheduled_at, "scheduled_at"))
        if self.amount_minor is None:
            if self.currency is not None or self.projected_daily_amount_minor is not None:
                raise ValueError("currency/daily money evidence requires amount_minor")
        else:
            object.__setattr__(self, "amount_minor", _minor(self.amount_minor, "amount_minor", zero=True))
            object.__setattr__(self, "currency", _currency(self.currency))
            if self.projected_daily_amount_minor is not None:
                object.__setattr__(
                    self,
                    "projected_daily_amount_minor",
                    _minor(self.projected_daily_amount_minor, "projected_daily_amount_minor", zero=True),
                )
        if self.ai_usage_minor is None:
            if self.ai_usage_currency is not None:
                raise ValueError("ai_usage_currency requires ai_usage_minor")
        else:
            object.__setattr__(self, "ai_usage_minor", _minor(self.ai_usage_minor, "ai_usage_minor", zero=True))
            object.__setattr__(self, "ai_usage_currency", _currency(self.ai_usage_currency, "ai_usage_currency"))
        for name in ("content_topics", "claims", "active_stop_conditions"):
            object.__setattr__(self, name, _tokens(list(getattr(self, name)), name))


@dataclass(frozen=True, slots=True)
class PolicyCheck:
    decision: PolicyDecision
    policy_id: str
    policy_version: int
    policy_hash: str
    violations: tuple[str, ...] = ()
    approval_reasons: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision == PolicyDecision.ALLOW

    @property
    def requires_approval(self) -> bool:
        return self.decision == PolicyDecision.APPROVAL_REQUIRED


def evaluate_automation_policy(
    *,
    policy: AutomationPolicy,
    candidate: AutomationCandidateAction,
    now: datetime | str,
) -> PolicyCheck:
    violations: list[str] = []
    approvals: list[str] = []
    if candidate.business_id != policy.business_id:
        violations.append("candidate_business_mismatch")
    if not policy.is_effective(now=now):
        violations.append("policy_not_effective")

    spec = policy.spec
    semantics = automation_action_semantics(candidate.action)
    if semantics is None:
        violations.append("action_semantics_unknown")
        effective_external_write = True
        money_bearing = True
    else:
        effective_external_write = semantics.external_write
        money_bearing = semantics.money_bearing
        if candidate.external_write != semantics.external_write:
            violations.append("action_semantics_mismatch")
    if candidate.action in spec.forbidden_actions:
        violations.append("action_forbidden")
    if candidate.action not in spec.allowed_actions:
        violations.append("action_not_explicitly_allowed")
    if effective_external_write and candidate.channel is None:
        violations.append("external_channel_required")
    elif candidate.channel is not None and candidate.channel not in spec.allowed_channels:
        violations.append("channel_not_allowed")
    if effective_external_write and candidate.audience is None:
        violations.append("external_audience_required")
    elif candidate.audience is not None and candidate.audience not in spec.allowed_audiences:
        violations.append("audience_not_allowed")

    scheduled = candidate.scheduled_at or _timestamp(now, "now")
    if not spec.schedule.permits(scheduled):
        violations.append("schedule_or_quiet_hours_block")

    active_stops = set(candidate.active_stop_conditions) & set(spec.stop_conditions)
    if active_stops:
        violations.append("stop_condition_active")
    if spec.allowed_content_topics and not set(candidate.content_topics).issubset(spec.allowed_content_topics):
        violations.append("content_topic_not_allowed")
    if set(candidate.claims) & set(spec.forbidden_claims):
        violations.append("forbidden_claim")

    action_money_limits = tuple(item for item in spec.money_limits if item.action == candidate.action)
    if money_bearing and candidate.amount_minor is None:
        violations.append("money_evidence_required")
    if money_bearing and not action_money_limits:
        violations.append("money_limit_missing")
    if candidate.amount_minor is not None:
        money_limit = next(
            (item for item in action_money_limits if item.currency == candidate.currency),
            None,
        )
        if money_limit is None:
            violations.append("money_limit_missing")
        else:
            if candidate.amount_minor > money_limit.max_per_action_minor:
                violations.append("money_per_action_limit_exceeded")
            if money_limit.max_daily_minor is not None:
                if candidate.projected_daily_amount_minor is None:
                    violations.append("daily_money_evidence_missing")
                elif candidate.projected_daily_amount_minor > money_limit.max_daily_minor:
                    violations.append("money_daily_limit_exceeded")

    if candidate.ai_usage_minor is not None:
        if spec.ai_usage_limit_minor is None or spec.ai_usage_currency is None:
            violations.append("ai_usage_limit_missing")
        elif candidate.ai_usage_currency != spec.ai_usage_currency:
            violations.append("ai_usage_currency_mismatch")
        elif candidate.ai_usage_minor > spec.ai_usage_limit_minor:
            violations.append("ai_usage_limit_exceeded")

    if effective_external_write and spec.mode == AutomationMode.CAUTIOUS:
        approvals.append("cautious_mode_external_write")
    if candidate.action in spec.approval_required_actions:
        approvals.append("action_requires_approval")
    if candidate.channel is not None and candidate.channel in spec.approval_required_channels:
        approvals.append("channel_requires_approval")
    for threshold in spec.approval_thresholds:
        if threshold.action != candidate.action:
            continue
        if threshold.amount_minor is None:
            approvals.append("action_threshold_requires_approval")
        elif (
            candidate.amount_minor is not None
            and candidate.currency == threshold.currency
            and candidate.amount_minor >= threshold.amount_minor
        ):
            approvals.append("money_threshold_requires_approval")

    if violations:
        decision = PolicyDecision.DENY
    elif approvals:
        decision = PolicyDecision.APPROVAL_REQUIRED
    else:
        decision = PolicyDecision.ALLOW
    return PolicyCheck(
        decision=decision,
        policy_id=policy.id,
        policy_version=policy.version,
        policy_hash=policy.policy_hash,
        violations=tuple(sorted(set(violations))),
        approval_reasons=tuple(sorted(set(approvals))),
    )


__all__ = [
    "AutomationActionSemantics",
    "AutomationApprovalThreshold",
    "AutomationCandidateAction",
    "AutomationMode",
    "AutomationMoneyLimit",
    "AutomationPolicy",
    "AutomationPolicyError",
    "AutomationPolicyConflict",
    "AutomationPolicyInvariantViolation",
    "AutomationPolicyNotFound",
    "AutomationPolicySpec",
    "AutomationPolicyStatus",
    "AutomationSchedule",
    "PolicyCheck",
    "PolicyDecision",
    "automation_action_semantics",
    "evaluate_automation_policy",
]
