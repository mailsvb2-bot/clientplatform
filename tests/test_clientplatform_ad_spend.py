from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from clientplatform.domain.ad_connections import AdProvider
from clientplatform.domain.ad_spend import (
    AdSpendAuthorization,
    AdSpendAuthorizationStatus,
    AdSpendConsentReceipt,
    AdSpendInvariantViolation,
    ProviderBudgetSnapshot,
)
from clientplatform.domain.tenancy import (
    PlatformRole,
    TenantAccessDenied,
    TenantContext,
)


_NOW = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


def _id() -> str:
    return str(uuid4())


def _owner(*, business_id: str | None = None) -> TenantContext:
    return TenantContext(
        business_id=business_id or _id(),
        user_id=101,
        membership_id=_id(),
        role=PlatformRole.OWNER,
    )


def _snapshot(*, connection_id: str | None = None) -> ProviderBudgetSnapshot:
    return ProviderBudgetSnapshot(
        provider=AdProvider.YANDEX_DIRECT,
        connection_id=connection_id or _id(),
        external_account_id="100500",
        external_campaign_id="6001",
        currency="rub",
        available_budget_minor=50_000,
        spent_today_minor=1_000,
        campaign_status="ON",
        strategy="HIGHEST_POSITION",
        launch_eligible=True,
        provider_version="campaign-v18",
        captured_at=_NOW,
        valid_until=_NOW + timedelta(minutes=15),
    )


def _draft(*, owner: TenantContext, snapshot: ProviderBudgetSnapshot | None = None):
    selected = snapshot or _snapshot()
    return AdSpendAuthorization.draft(
        authorization_id=_id(),
        business_id=owner.business_id,
        publication_job_id=_id(),
        region_ids=(47, 213),
        hard_cap_minor=20_000,
        daily_cap_minor=5_000,
        authorization_expires_at=_NOW + timedelta(minutes=10),
        snapshot=selected,
        created_by_member_id=owner.membership_id,
        now=_NOW,
    )


class ProviderBudgetSnapshotTests(unittest.TestCase):
    def test_snapshot_is_canonical_and_stale_data_fails_closed(self) -> None:
        snapshot = _snapshot()
        self.assertTrue(snapshot.snapshot_hash.startswith("adsnap_"))
        self.assertEqual(snapshot.currency, "RUB")
        snapshot.assert_fresh(now=_NOW + timedelta(minutes=14))
        with self.assertRaisesRegex(AdSpendInvariantViolation, "snapshot is stale"):
            snapshot.assert_fresh(now=_NOW + timedelta(minutes=15))

    def test_snapshot_hash_changes_when_provider_budget_changes(self) -> None:
        snapshot = _snapshot()
        changed = replace(snapshot, available_budget_minor=49_999)
        self.assertNotEqual(snapshot.snapshot_hash, changed.snapshot_hash)


class ConsentBoundSpendStateMachineTests(unittest.TestCase):
    def test_owner_consent_creates_immutable_verifiable_receipt(self) -> None:
        owner = _owner()
        draft = _draft(owner=owner)
        awaiting = draft.request_consent(actor=owner, now=_NOW + timedelta(seconds=10))
        authorization, receipt = awaiting.authorize(
            actor=owner,
            receipt_id=_id(),
            now=_NOW + timedelta(seconds=20),
        )

        self.assertEqual(
            authorization.status,
            AdSpendAuthorizationStatus.AUTHORIZED,
        )
        self.assertEqual(authorization.consent_receipt, receipt)
        self.assertEqual(receipt.business_id, owner.business_id)
        self.assertEqual(receipt.actor_member_id, owner.membership_id)
        self.assertEqual(receipt.terms_hash, authorization.terms_hash)
        self.assertEqual(receipt.receipt_hash, receipt.expected_receipt_hash())
        with self.assertRaises(FrozenInstanceError):
            receipt.receipt_hash = "tampered"  # type: ignore[misc]

    def test_non_owner_and_cross_tenant_consent_are_rejected(self) -> None:
        owner = _owner()
        awaiting = _draft(owner=owner).request_consent(actor=owner, now=_NOW)
        administrator = TenantContext(
            business_id=owner.business_id,
            user_id=202,
            membership_id=_id(),
            role=PlatformRole.ADMINISTRATOR,
        )
        with self.assertRaisesRegex(AdSpendInvariantViolation, "owner consent"):
            awaiting.authorize(
                actor=administrator,
                receipt_id=_id(),
                now=_NOW + timedelta(seconds=1),
            )

        other_owner = _owner()
        with self.assertRaisesRegex(TenantAccessDenied, "another business"):
            awaiting.authorize(
                actor=other_owner,
                receipt_id=_id(),
                now=_NOW + timedelta(seconds=1),
            )

    def test_launch_is_impossible_without_receipt_or_with_stale_snapshot(self) -> None:
        owner = _owner()
        draft = _draft(owner=owner)
        with self.assertRaisesRegex(AdSpendInvariantViolation, "owner authorization"):
            draft.claim_launch(now=_NOW + timedelta(seconds=1))

        awaiting = draft.request_consent(actor=owner, now=_NOW + timedelta(seconds=1))
        authorized, _ = awaiting.authorize(
            actor=owner,
            receipt_id=_id(),
            now=_NOW + timedelta(seconds=2),
        )
        launching = authorized.claim_launch(now=_NOW + timedelta(minutes=5))
        active = launching.mark_active(now=_NOW + timedelta(minutes=6))
        stopping = active.begin_stop(now=_NOW + timedelta(minutes=7))
        stopped = stopping.mark_stopped(now=_NOW + timedelta(minutes=8))
        self.assertEqual(stopped.status, AdSpendAuthorizationStatus.STOPPED)

        with self.assertRaisesRegex(AdSpendInvariantViolation, "snapshot is stale"):
            authorized.claim_launch(now=_NOW + timedelta(minutes=15))

    def test_caps_and_validity_cannot_exceed_provider_snapshot(self) -> None:
        owner = _owner()
        snapshot = _snapshot()
        with self.assertRaisesRegex(AdSpendInvariantViolation, "hard cap exceeds"):
            AdSpendAuthorization.draft(
                authorization_id=_id(),
                business_id=owner.business_id,
                publication_job_id=_id(),
                region_ids=(47,),
                hard_cap_minor=50_001,
                daily_cap_minor=5_000,
                authorization_expires_at=_NOW + timedelta(minutes=10),
                snapshot=snapshot,
                created_by_member_id=owner.membership_id,
                now=_NOW,
            )
        with self.assertRaisesRegex(AdSpendInvariantViolation, "exceeds snapshot validity"):
            AdSpendAuthorization.draft(
                authorization_id=_id(),
                business_id=owner.business_id,
                publication_job_id=_id(),
                region_ids=(47,),
                hard_cap_minor=20_000,
                daily_cap_minor=5_000,
                authorization_expires_at=_NOW + timedelta(minutes=16),
                snapshot=snapshot,
                created_by_member_id=owner.membership_id,
                now=_NOW,
            )

    def test_tampered_receipt_is_rejected(self) -> None:
        owner = _owner()
        awaiting = _draft(owner=owner).request_consent(actor=owner, now=_NOW)
        _, receipt = awaiting.authorize(
            actor=owner,
            receipt_id=_id(),
            now=_NOW + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(AdSpendInvariantViolation, "receipt hash"):
            AdSpendConsentReceipt(
                id=receipt.id,
                business_id=receipt.business_id,
                authorization_id=receipt.authorization_id,
                actor_member_id=receipt.actor_member_id,
                actor_user_id=receipt.actor_user_id,
                terms_json=receipt.terms_json,
                terms_hash=receipt.terms_hash,
                snapshot_hash=receipt.snapshot_hash,
                consented_at=receipt.consented_at,
                receipt_hash="adconsent_" + "0" * 64,
            )

    def test_expiry_and_revocation_are_terminal_fail_closed_states(self) -> None:
        owner = _owner()
        awaiting = _draft(owner=owner).request_consent(actor=owner, now=_NOW)
        revoked = awaiting.revoke(actor=owner, now=_NOW + timedelta(seconds=1))
        self.assertEqual(revoked.status, AdSpendAuthorizationStatus.REVOKED)
        with self.assertRaisesRegex(AdSpendInvariantViolation, "terminal"):
            revoked.revoke(actor=owner, now=_NOW + timedelta(seconds=2))

        expired = _draft(owner=owner).expire(now=_NOW + timedelta(minutes=10))
        self.assertEqual(expired.status, AdSpendAuthorizationStatus.EXPIRED)


if __name__ == "__main__":
    unittest.main()
