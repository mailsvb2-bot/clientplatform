from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum

from clientplatform.domain.ad_connections import (
    AdProvider,
    normalize_external_account_id,
    normalize_external_campaign_id,
    normalize_region_ids,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext, normalize_uuid


class AdSpendError(RuntimeError):
    """Base error for consent-bound advertising spend operations."""


class AdSpendInvariantViolation(AdSpendError):
    """A spend operation violates a fail-closed safety invariant."""


class AdSpendAuthorizationStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_CONSENT = "awaiting_consent"
    AUTHORIZED = "authorized"
    LAUNCHING = "launching"
    ACTIVE = "active"
    STOPPING = "stopping"
    STOPPED = "stopped"
    EXPIRED = "expired"
    REVOKED = "revoked"
    FAILED = "failed"


class AdSpendStopCondition(StrEnum):
    HARD_CAP_OR_DAILY_CAP_OR_EXPIRY = "hard_cap_or_daily_cap_or_expiry"


def _dt(value: datetime | str, name: str) -> datetime:
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
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | str, name: str) -> str:
    return _dt(value, name).isoformat(timespec="seconds")


def _money(value: object, name: str, *, zero: bool = False) -> int:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{name} must use integer minor units")
    try:
        amount = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must use integer minor units") from exc
    if amount < (0 if zero else 1) or amount > 9_000_000_000_000_000:
        raise ValueError(f"{name} is outside the supported range")
    return amount


def _token(value: object, name: str, limit: int = 160) -> str:
    token = str(value or "").strip()
    if not token or len(token) > limit or "\x00" in token:
        raise ValueError(f"{name} is invalid")
    return token


def _digest(prefix: str, payload: dict[str, object]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return prefix + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderBudgetSnapshot:
    provider: AdProvider
    connection_id: str
    external_account_id: str
    external_campaign_id: str
    currency: str
    available_budget_minor: int
    spent_today_minor: int
    campaign_status: str
    strategy: str
    launch_eligible: bool
    provider_version: str
    captured_at: str
    valid_until: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", AdProvider(self.provider))
        object.__setattr__(
            self,
            "connection_id",
            normalize_uuid(self.connection_id, field_name="connection_id"),
        )
        object.__setattr__(
            self,
            "external_account_id",
            normalize_external_account_id(self.external_account_id),
        )
        object.__setattr__(
            self,
            "external_campaign_id",
            normalize_external_campaign_id(self.external_campaign_id),
        )
        currency = str(self.currency or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("currency must be a three-letter ISO code")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(
            self,
            "available_budget_minor",
            _money(self.available_budget_minor, "available_budget_minor", zero=True),
        )
        object.__setattr__(
            self,
            "spent_today_minor",
            _money(self.spent_today_minor, "spent_today_minor", zero=True),
        )
        object.__setattr__(
            self,
            "campaign_status",
            _token(self.campaign_status, "campaign_status", 80),
        )
        object.__setattr__(self, "strategy", _token(self.strategy, "strategy"))
        if not isinstance(self.launch_eligible, bool):
            raise ValueError("launch_eligible must be provider-derived boolean")
        object.__setattr__(
            self,
            "provider_version",
            _token(self.provider_version, "provider_version"),
        )
        captured = _dt(self.captured_at, "captured_at")
        valid_until = _dt(self.valid_until, "valid_until")
        if valid_until <= captured:
            raise ValueError("snapshot validity must end after capture")
        object.__setattr__(self, "captured_at", _iso(captured, "captured_at"))
        object.__setattr__(self, "valid_until", _iso(valid_until, "valid_until"))

    def payload(self) -> dict[str, object]:
        return {
            "provider": self.provider.value,
            "connection_id": self.connection_id,
            "external_account_id": self.external_account_id,
            "external_campaign_id": self.external_campaign_id,
            "currency": self.currency,
            "available_budget_minor": self.available_budget_minor,
            "spent_today_minor": self.spent_today_minor,
            "campaign_status": self.campaign_status,
            "strategy": self.strategy,
            "launch_eligible": self.launch_eligible,
            "provider_version": self.provider_version,
            "captured_at": self.captured_at,
            "valid_until": self.valid_until,
        }

    @property
    def snapshot_hash(self) -> str:
        return _digest("adsnap_", self.payload())

    def assert_fresh(self, *, now: datetime | str) -> None:
        current = _dt(now, "now")
        if current < _dt(self.captured_at, "captured_at"):
            raise AdSpendInvariantViolation("provider budget snapshot is from future")
        if current >= _dt(self.valid_until, "valid_until"):
            raise AdSpendInvariantViolation("provider budget snapshot is stale")


@dataclass(frozen=True, slots=True)
class AdSpendConsentReceipt:
    id: str
    business_id: str
    authorization_id: str
    actor_member_id: str
    actor_user_id: int
    terms_json: str
    terms_hash: str
    snapshot_hash: str
    consented_at: str
    receipt_hash: str
    version: str = "1"

    def __post_init__(self) -> None:
        for name in ("id", "business_id", "authorization_id", "actor_member_id"):
            object.__setattr__(
                self,
                name,
                normalize_uuid(getattr(self, name), field_name=name),
            )
        if isinstance(self.actor_user_id, bool) or int(self.actor_user_id) <= 0:
            raise ValueError("actor_user_id must be positive")
        object.__setattr__(self, "actor_user_id", int(self.actor_user_id))
        try:
            terms = json.loads(self.terms_json)
        except json.JSONDecodeError as exc:
            raise ValueError("consent terms must be valid JSON") from exc
        canonical = json.dumps(
            terms,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        object.__setattr__(self, "terms_json", canonical)
        object.__setattr__(self, "consented_at", _iso(self.consented_at, "consented_at"))
        patterns = (
            (self.terms_hash, r"adterms_[0-9a-f]{64}"),
            (self.snapshot_hash, r"adsnap_[0-9a-f]{64}"),
            (self.receipt_hash, r"adconsent_[0-9a-f]{64}"),
        )
        if any(not re.fullmatch(pattern, value) for value, pattern in patterns):
            raise ValueError("consent hash format is invalid")
        if self.version != "1":
            raise ValueError("unsupported consent receipt version")
        if _digest("adterms_", terms) != self.terms_hash:
            raise AdSpendInvariantViolation("consent terms hash does not match terms")
        if self.expected_hash() != self.receipt_hash:
            raise AdSpendInvariantViolation("consent receipt hash is invalid")

    def expected_hash(self) -> str:
        return _digest(
            "adconsent_",
            {
                "id": self.id,
                "business_id": self.business_id,
                "authorization_id": self.authorization_id,
                "actor_member_id": self.actor_member_id,
                "actor_user_id": self.actor_user_id,
                "terms_hash": self.terms_hash,
                "snapshot_hash": self.snapshot_hash,
                "consented_at": self.consented_at,
                "version": self.version,
            },
        )

    def expected_receipt_hash(self) -> str:
        return self.expected_hash()


_SPEND_CAPABLE = frozenset(
    {
        AdSpendAuthorizationStatus.AUTHORIZED,
        AdSpendAuthorizationStatus.LAUNCHING,
        AdSpendAuthorizationStatus.ACTIVE,
        AdSpendAuthorizationStatus.STOPPING,
        AdSpendAuthorizationStatus.STOPPED,
    }
)
_TERMINAL = frozenset(
    {
        AdSpendAuthorizationStatus.STOPPED,
        AdSpendAuthorizationStatus.EXPIRED,
        AdSpendAuthorizationStatus.REVOKED,
        AdSpendAuthorizationStatus.FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class AdSpendAuthorization:
    id: str
    business_id: str
    connection_id: str
    publication_job_id: str
    external_campaign_id: str
    region_ids: tuple[int, ...]
    currency: str
    hard_cap_minor: int
    daily_cap_minor: int
    authorization_expires_at: str
    stop_condition: AdSpendStopCondition
    snapshot: ProviderBudgetSnapshot
    status: AdSpendAuthorizationStatus
    created_by_member_id: str
    created_at: str
    updated_at: str
    consent_receipt: AdSpendConsentReceipt | None = None
    revoked_at: str | None = None
    stopped_at: str | None = None
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "id",
            "business_id",
            "connection_id",
            "publication_job_id",
            "created_by_member_id",
        ):
            object.__setattr__(
                self,
                name,
                normalize_uuid(getattr(self, name), field_name=name),
            )
        object.__setattr__(
            self,
            "external_campaign_id",
            normalize_external_campaign_id(self.external_campaign_id),
        )
        object.__setattr__(self, "region_ids", normalize_region_ids(self.region_ids))
        object.__setattr__(self, "currency", str(self.currency).strip().upper())
        object.__setattr__(
            self,
            "hard_cap_minor",
            _money(self.hard_cap_minor, "hard_cap_minor"),
        )
        object.__setattr__(
            self,
            "daily_cap_minor",
            _money(self.daily_cap_minor, "daily_cap_minor"),
        )
        if self.daily_cap_minor > self.hard_cap_minor:
            raise ValueError("daily cap must not exceed hard cap")
        object.__setattr__(
            self,
            "authorization_expires_at",
            _iso(self.authorization_expires_at, "authorization_expires_at"),
        )
        object.__setattr__(self, "stop_condition", AdSpendStopCondition(self.stop_condition))
        object.__setattr__(self, "status", AdSpendAuthorizationStatus(self.status))
        object.__setattr__(self, "created_at", _iso(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _iso(self.updated_at, "updated_at"))
        for name in ("revoked_at", "stopped_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _iso(value, name))
        if self.last_error_code is not None:
            object.__setattr__(
                self,
                "last_error_code",
                _token(self.last_error_code, "last_error_code", 120),
            )
        self._validate_snapshot()
        self._validate_receipt()

    def _validate_snapshot(self) -> None:
        snapshot = self.snapshot
        if snapshot.connection_id != self.connection_id:
            raise AdSpendInvariantViolation("snapshot connection does not match")
        if snapshot.external_campaign_id != self.external_campaign_id:
            raise AdSpendInvariantViolation("snapshot campaign does not match")
        if snapshot.currency != self.currency:
            raise AdSpendInvariantViolation("snapshot currency does not match")
        if not snapshot.launch_eligible:
            raise AdSpendInvariantViolation("provider snapshot is not launch-eligible")
        if self.hard_cap_minor > snapshot.available_budget_minor:
            raise AdSpendInvariantViolation("hard cap exceeds provider budget")
        if _dt(self.authorization_expires_at, "authorization_expires_at") > _dt(
            snapshot.valid_until,
            "snapshot.valid_until",
        ):
            raise AdSpendInvariantViolation("authorization exceeds snapshot validity")

    def _validate_receipt(self) -> None:
        receipt = self.consent_receipt
        if self.status in _SPEND_CAPABLE and receipt is None:
            raise AdSpendInvariantViolation("spend-capable state requires consent receipt")
        if receipt is None:
            return
        if receipt.business_id != self.business_id:
            raise AdSpendInvariantViolation("consent receipt belongs to another business")
        if receipt.authorization_id != self.id:
            raise AdSpendInvariantViolation("consent receipt belongs to another authorization")
        if receipt.snapshot_hash != self.snapshot.snapshot_hash:
            raise AdSpendInvariantViolation("consent receipt snapshot does not match")
        if receipt.terms_hash != self.terms_hash:
            raise AdSpendInvariantViolation("consent receipt terms do not match")

    @classmethod
    def draft(
        cls,
        *,
        authorization_id: str,
        business_id: str,
        publication_job_id: str,
        region_ids: tuple[int, ...],
        hard_cap_minor: int,
        daily_cap_minor: int,
        authorization_expires_at: datetime | str,
        snapshot: ProviderBudgetSnapshot,
        created_by_member_id: str,
        now: datetime | str,
    ) -> AdSpendAuthorization:
        timestamp = _iso(now, "now")
        snapshot.assert_fresh(now=timestamp)
        expires = _iso(authorization_expires_at, "authorization_expires_at")
        if _dt(expires, "authorization_expires_at") <= _dt(timestamp, "now"):
            raise AdSpendInvariantViolation("authorization must expire in future")
        return cls(
            id=authorization_id,
            business_id=business_id,
            connection_id=snapshot.connection_id,
            publication_job_id=publication_job_id,
            external_campaign_id=snapshot.external_campaign_id,
            region_ids=region_ids,
            currency=snapshot.currency,
            hard_cap_minor=hard_cap_minor,
            daily_cap_minor=daily_cap_minor,
            authorization_expires_at=expires,
            stop_condition=AdSpendStopCondition.HARD_CAP_OR_DAILY_CAP_OR_EXPIRY,
            snapshot=snapshot,
            status=AdSpendAuthorizationStatus.DRAFT,
            created_by_member_id=created_by_member_id,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def terms_payload(self) -> dict[str, object]:
        return {
            "authorization_id": self.id,
            "business_id": self.business_id,
            "connection_id": self.connection_id,
            "publication_job_id": self.publication_job_id,
            "external_campaign_id": self.external_campaign_id,
            "region_ids": list(self.region_ids),
            "currency": self.currency,
            "hard_cap_minor": self.hard_cap_minor,
            "daily_cap_minor": self.daily_cap_minor,
            "authorization_expires_at": self.authorization_expires_at,
            "stop_condition": self.stop_condition.value,
            "snapshot_hash": self.snapshot.snapshot_hash,
        }

    @property
    def terms_hash(self) -> str:
        return _digest("adterms_", self.terms_payload())

    def _owner(self, actor: TenantContext) -> None:
        actor.assert_business(self.business_id)
        if actor.role != PlatformRole.OWNER:
            raise AdSpendInvariantViolation("advertising spend requires owner consent")

    def _live(self, now: datetime | str) -> str:
        timestamp = _iso(now, "now")
        self.snapshot.assert_fresh(now=timestamp)
        if _dt(timestamp, "now") >= _dt(
            self.authorization_expires_at,
            "authorization_expires_at",
        ):
            raise AdSpendInvariantViolation("advertising spend authorization is expired")
        return timestamp

    def request_consent(
        self,
        *,
        actor: TenantContext,
        now: datetime | str,
    ) -> AdSpendAuthorization:
        self._owner(actor)
        timestamp = self._live(now)
        if self.status != AdSpendAuthorizationStatus.DRAFT:
            raise AdSpendInvariantViolation("only draft can request consent")
        return replace(
            self,
            status=AdSpendAuthorizationStatus.AWAITING_CONSENT,
            updated_at=timestamp,
        )

    def authorize(
        self,
        *,
        actor: TenantContext,
        receipt_id: str,
        now: datetime | str,
    ) -> tuple[AdSpendAuthorization, AdSpendConsentReceipt]:
        self._owner(actor)
        timestamp = self._live(now)
        if self.status != AdSpendAuthorizationStatus.AWAITING_CONSENT:
            raise AdSpendInvariantViolation("authorization is not awaiting consent")
        receipt_id = normalize_uuid(receipt_id, field_name="consent_receipt_id")
        terms = self.terms_payload()
        terms_json = json.dumps(
            terms,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        terms_hash = _digest("adterms_", terms)
        base = {
            "id": receipt_id,
            "business_id": self.business_id,
            "authorization_id": self.id,
            "actor_member_id": actor.membership_id,
            "actor_user_id": actor.user_id,
            "terms_hash": terms_hash,
            "snapshot_hash": self.snapshot.snapshot_hash,
            "consented_at": timestamp,
            "version": "1",
        }
        receipt = AdSpendConsentReceipt(
            terms_json=terms_json,
            receipt_hash=_digest("adconsent_", base),
            **base,
        )
        return (
            replace(
                self,
                status=AdSpendAuthorizationStatus.AUTHORIZED,
                consent_receipt=receipt,
                updated_at=timestamp,
            ),
            receipt,
        )

    def claim_launch(self, *, now: datetime | str) -> AdSpendAuthorization:
        timestamp = self._live(now)
        if self.status != AdSpendAuthorizationStatus.AUTHORIZED:
            raise AdSpendInvariantViolation("launch requires owner authorization")
        return replace(
            self,
            status=AdSpendAuthorizationStatus.LAUNCHING,
            updated_at=timestamp,
        )

    def mark_active(self, *, now: datetime | str) -> AdSpendAuthorization:
        timestamp = self._live(now)
        if self.status != AdSpendAuthorizationStatus.LAUNCHING:
            raise AdSpendInvariantViolation("only launching can become active")
        return replace(
            self,
            status=AdSpendAuthorizationStatus.ACTIVE,
            updated_at=timestamp,
        )

    def begin_stop(self, *, now: datetime | str) -> AdSpendAuthorization:
        if self.status not in {
            AdSpendAuthorizationStatus.LAUNCHING,
            AdSpendAuthorizationStatus.ACTIVE,
        }:
            raise AdSpendInvariantViolation("only launching or active can stop")
        timestamp = _iso(now, "now")
        return replace(
            self,
            status=AdSpendAuthorizationStatus.STOPPING,
            updated_at=timestamp,
        )

    def mark_stopped(self, *, now: datetime | str) -> AdSpendAuthorization:
        if self.status != AdSpendAuthorizationStatus.STOPPING:
            raise AdSpendInvariantViolation("only stopping can become stopped")
        timestamp = _iso(now, "now")
        return replace(
            self,
            status=AdSpendAuthorizationStatus.STOPPED,
            stopped_at=timestamp,
            updated_at=timestamp,
        )

    def revoke(
        self,
        *,
        actor: TenantContext,
        now: datetime | str,
    ) -> AdSpendAuthorization:
        self._owner(actor)
        if self.status in _TERMINAL:
            raise AdSpendInvariantViolation("terminal authorization cannot be revoked")
        timestamp = _iso(now, "now")
        return replace(
            self,
            status=AdSpendAuthorizationStatus.REVOKED,
            revoked_at=timestamp,
            updated_at=timestamp,
        )

    def expire(self, *, now: datetime | str) -> AdSpendAuthorization:
        timestamp = _iso(now, "now")
        if _dt(timestamp, "now") < _dt(
            self.authorization_expires_at,
            "authorization_expires_at",
        ):
            raise AdSpendInvariantViolation("authorization has not expired")
        if self.status in _TERMINAL:
            raise AdSpendInvariantViolation("terminal authorization cannot expire")
        return replace(
            self,
            status=AdSpendAuthorizationStatus.EXPIRED,
            updated_at=timestamp,
        )

    def fail(self, *, error_code: str, now: datetime | str) -> AdSpendAuthorization:
        if self.status not in {
            AdSpendAuthorizationStatus.LAUNCHING,
            AdSpendAuthorizationStatus.STOPPING,
        }:
            raise AdSpendInvariantViolation("only provider mutation can fail")
        return replace(
            self,
            status=AdSpendAuthorizationStatus.FAILED,
            last_error_code=_token(error_code, "error_code", 120),
            updated_at=_iso(now, "now"),
        )


__all__ = [
    "AdSpendAuthorization",
    "AdSpendAuthorizationStatus",
    "AdSpendConsentReceipt",
    "AdSpendError",
    "AdSpendInvariantViolation",
    "AdSpendStopCondition",
    "ProviderBudgetSnapshot",
]
