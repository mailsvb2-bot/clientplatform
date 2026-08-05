from __future__ import annotations

import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from clientplatform.domain.ad_spend import AdSpendInvariantViolation
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.infrastructure import ad_spend_preparation_repository as module


def _context(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        user_id=101,
        membership_id=str(uuid4()),
        role=role,
    )


def _row(owner: TenantContext, **overrides):
    values = {
        "business_id": owner.business_id,
        "publication_job_id": str(uuid4()),
        "connection_id": str(uuid4()),
        "external_account_id": "100500",
        "external_login": "vasya",
        "external_campaign_id": "6001",
        "region_ids_json": "[47, 213]",
        "job_status": "submitted",
        "connection_status": "active",
        "provider": "yandex_direct",
    }
    values.update(overrides)
    return values


class AdSpendPreparationRepositoryTests(unittest.TestCase):
    def _repository(self, *, current: TenantContext, row):
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = row
        tenancy = Mock()
        tenancy.resolve_context.return_value = current
        patcher = patch.object(module, "TenancyRepository", return_value=tenancy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return module.AdSpendPreparationRepository(conn), conn, tenancy

    def test_owner_loads_only_submitted_active_yandex_target(self) -> None:
        owner = _context()
        row = _row(owner)
        repository, conn, tenancy = self._repository(current=owner, row=row)

        current, target = repository.load_submitted_target(
            actor=owner,
            publication_job_id=row["publication_job_id"],
        )

        self.assertIs(current, owner)
        self.assertEqual(target.business_id, owner.business_id)
        self.assertEqual(target.publication_job_id, row["publication_job_id"])
        self.assertEqual(target.connection_id, row["connection_id"])
        self.assertEqual(target.external_account_id, "100500")
        self.assertEqual(target.external_login, "vasya")
        self.assertEqual(target.external_campaign_id, "6001")
        self.assertEqual(target.region_ids, (47, 213))
        tenancy.resolve_context.assert_called_once_with(
            user_id=owner.user_id,
            business_id=owner.business_id,
        )
        statement, params = conn.execute.call_args.args
        self.assertIn("j.business_id", statement)
        self.assertIn("c.business_id=j.business_id", statement)
        self.assertEqual(params, (row["publication_job_id"], owner.business_id))

    def test_non_owner_is_rejected_before_query(self) -> None:
        administrator = _context(PlatformRole.ADMINISTRATOR)
        repository, conn, _ = self._repository(current=administrator, row=None)
        with self.assertRaisesRegex(AdSpendInvariantViolation, "owner role"):
            repository.load_submitted_target(
                actor=administrator,
                publication_job_id=str(uuid4()),
            )
        conn.execute.assert_not_called()

    def test_missing_or_non_submitted_job_fails_closed(self) -> None:
        owner = _context()
        missing, _, _ = self._repository(current=owner, row=None)
        with self.assertRaisesRegex(AdSpendInvariantViolation, "not found for business"):
            missing.load_submitted_target(
                actor=owner,
                publication_job_id=str(uuid4()),
            )

        draft_row = _row(owner, job_status="draft")
        draft, _, _ = self._repository(current=owner, row=draft_row)
        with self.assertRaisesRegex(AdSpendInvariantViolation, "provider-created DRAFT"):
            draft.load_submitted_target(
                actor=owner,
                publication_job_id=draft_row["publication_job_id"],
            )

    def test_disabled_connection_or_unknown_provider_fails_closed(self) -> None:
        owner = _context()
        disabled_row = _row(owner, connection_status="disabled")
        disabled, _, _ = self._repository(current=owner, row=disabled_row)
        with self.assertRaisesRegex(AdSpendInvariantViolation, "not active"):
            disabled.load_submitted_target(
                actor=owner,
                publication_job_id=disabled_row["publication_job_id"],
            )

        provider_row = _row(owner, provider="unknown-provider")
        unsupported, _, _ = self._repository(current=owner, row=provider_row)
        with self.assertRaisesRegex(AdSpendInvariantViolation, "unsupported"):
            unsupported.load_submitted_target(
                actor=owner,
                publication_job_id=provider_row["publication_job_id"],
            )

    def test_corrupt_regions_or_target_identity_fails_closed(self) -> None:
        owner = _context()
        corrupt_row = _row(owner, region_ids_json="not-json")
        corrupt, _, _ = self._repository(current=owner, row=corrupt_row)
        with self.assertRaisesRegex(AdSpendInvariantViolation, "regions are invalid"):
            corrupt.load_submitted_target(
                actor=owner,
                publication_job_id=corrupt_row["publication_job_id"],
            )

        identity_row = _row(owner, external_account_id="")
        invalid, _, _ = self._repository(current=owner, row=identity_row)
        with self.assertRaisesRegex(AdSpendInvariantViolation, "account identity"):
            invalid.load_submitted_target(
                actor=owner,
                publication_job_id=identity_row["publication_job_id"],
            )


if __name__ == "__main__":
    unittest.main()
