from __future__ import annotations

import sqlite3
import unittest

from clientplatform.domain.commercial_ladder import (
    CommercialLadderStep,
    CommercialStepKind,
    eligible_offer_candidates,
)
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.commercial_ladder_repository import (
    CommercialLadderRepository,
)
from services.db.schema import (
    clientplatform_activity,
    clientplatform_offer_ladders,
    clientplatform_tenancy,
)


class ClientPlatformCommercialLadderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        clientplatform_tenancy.ensure(self.conn)
        clientplatform_activity.ensure(self.conn)
        clientplatform_offer_ladders.ensure(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = tenancy.resolve_context(
            user_id=101, business_id=access.business.id
        )
        self.repo = CommercialLadderRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()


    def test_ladder_read_uses_promotion_analytics_permission(self) -> None:
        ladder = self.repo.create_ladder(actor=self.owner, name="Для аналитики")
        self.repo.add_step(
            actor=self.owner,
            ladder_id=ladder,
            position=0,
            kind=CommercialStepKind.DIAGNOSTIC,
            title="Диагностика",
            requires_human_approval=False,
        )
        self.assertEqual(len(self.repo.list_steps(actor=self.owner, ladder_id=ladder)), 1)

    def test_ladder_exposes_candidates_without_choosing_for_decision_layer(self) -> None:
        ladder = self.repo.create_ladder(actor=self.owner, name="Основная лестница")
        diagnostic = self.repo.add_step(
            actor=self.owner,
            ladder_id=ladder,
            position=0,
            kind=CommercialStepKind.DIAGNOSTIC,
            title="Бесплатная диагностика",
            min_evidence_score=0.0,
            requires_human_approval=False,
        )
        audit = self.repo.add_step(
            actor=self.owner,
            ladder_id=ladder,
            position=1,
            kind=CommercialStepKind.AUDIT,
            title="Платный аудит",
            min_evidence_score=0.55,
        )
        implementation = self.repo.add_step(
            actor=self.owner,
            ladder_id=ladder,
            position=2,
            kind=CommercialStepKind.IMPLEMENTATION,
            title="Внедрение",
            min_evidence_score=0.8,
        )
        early = self.repo.candidates(
            actor=self.owner, ladder_id=ladder, evidence_score=0.6
        )
        self.assertEqual(
            [item.step_id for item in early],
            [diagnostic.id, audit.id],
        )
        mature = self.repo.candidates(
            actor=self.owner,
            ladder_id=ladder,
            evidence_score=0.95,
            completed_step_ids={diagnostic.id, audit.id},
        )
        self.assertEqual([item.step_id for item in mature], [implementation.id])

    def test_non_finite_evidence_fails_closed_and_position_is_normalized(self) -> None:
        from uuid import uuid4

        step = CommercialLadderStep(
            id=str(uuid4()),
            business_id=self.owner.business_id,
            ladder_id=str(uuid4()),
            position="1",
            kind="audit",
            title="Аудит",
            offering_id=None,
            min_evidence_score=0.5,
            requires_human_approval=True,
        )
        self.assertEqual(step.position, 1)
        self.assertIsInstance(step.position, int)
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaisesRegex(ValueError, "finite"):
                eligible_offer_candidates([step], evidence_score=value)
            with self.assertRaisesRegex(ValueError, "finite"):
                CommercialLadderStep(
                    id=str(uuid4()),
                    business_id=self.owner.business_id,
                    ladder_id=str(uuid4()),
                    position=0,
                    kind="audit",
                    title="Аудит",
                    offering_id=None,
                    min_evidence_score=value,
                    requires_human_approval=True,
                )

    def test_missing_ladder_does_not_look_like_empty_ladder(self) -> None:
        from uuid import uuid4

        with self.assertRaisesRegex(ValueError, "not found"):
            self.repo.list_steps(actor=self.owner, ladder_id=str(uuid4()))

    def test_fractional_position_is_rejected_instead_of_truncated(self) -> None:
        ladder_id = self.repo.create_ladder(actor=self.owner, name="Strict positions")
        with self.assertRaisesRegex(ValueError, "position"):
            self.repo.add_step(
                actor=self.owner, ladder_id=ladder_id, position=1.5,
                kind=CommercialStepKind.DIAGNOSTIC, title="Audit",
                min_evidence_score=0.0, requires_human_approval=True,
            )



if __name__ == "__main__":
    unittest.main()
