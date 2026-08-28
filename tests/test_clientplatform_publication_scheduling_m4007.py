from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from clientplatform.application.activity import save_business_profile
from clientplatform.application.admin_ops import (
    cancel_publication_schedule,
    create_publication_draft,
    publish_publication,
    schedule_publication,
)
from clientplatform.application.tenancy import (
    create_business,
    grant_business_member,
    resolve_tenant_context,
)
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from services.db import get_db, get_db_ro
from services.db import core as db_core
from services.schema import init_db


def _actor(user_id: int, name: str, *, timezone_name: str = "Europe/Moscow"):
    access = create_business(owner_user_id=user_id, name=name)
    actor = resolve_tenant_context(user_id=user_id, business_id=access.business.id)
    save_business_profile(
        actor=actor,
        activity_description=f"Деятельность {name}",
        timezone_name=timezone_name,
    )
    return actor


def _audit_count(business_id: str, publication_id: str, action: str) -> int:
    with get_db_ro() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM clientplatform_admin_audit_events
            WHERE business_id=? AND subject_id=? AND action=?
            """,
            (business_id, publication_id, action),
        ).fetchone()
    return int(row["c"])


class PublicationSchedulingM4007Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_db_path = db_core.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory(prefix="clientplatform-m4007-")
        db_core.DB_PATH = Path(self._tmpdir.name) / "publication-scheduling.db"
        init_db()

    def tearDown(self) -> None:
        db_core.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    def test_schedule_reschedule_and_cancel_are_canonical_and_idempotent(self) -> None:
        actor = _actor(840701, "Планирование публикаций")
        draft = create_publication_draft(
            actor=actor,
            title="План",
            body="Текст",
            channel="vk",
        )
        now = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)

        scheduled = schedule_publication(
            actor=actor,
            publication_id=draft.id,
            local_time="29.08.2026 12:00",
            now=now,
        )
        assert scheduled.status == "scheduled"
        assert scheduled.scheduled_at == "2026-08-29T09:00:00+00:00"
        first_updated_at = scheduled.updated_at
        assert _audit_count(actor.business_id, draft.id, "publication_scheduled") == 1

        repeated = schedule_publication(
            actor=actor,
            publication_id=draft.id,
            local_time="29.08.2026 12:00",
            now=now,
        )
        assert repeated == scheduled
        assert repeated.updated_at == first_updated_at
        assert _audit_count(actor.business_id, draft.id, "publication_scheduled") == 1

        moved = schedule_publication(
            actor=actor,
            publication_id=draft.id,
            local_time="30.08.2026 15:30",
            now=now,
        )
        assert moved.status == "scheduled"
        assert moved.scheduled_at == "2026-08-30T12:30:00+00:00"
        assert _audit_count(actor.business_id, draft.id, "publication_rescheduled") == 1

        cancelled = cancel_publication_schedule(actor=actor, publication_id=draft.id)
        assert cancelled.status == "cancelled"
        assert cancelled.scheduled_at == moved.scheduled_at
        assert _audit_count(actor.business_id, draft.id, "publication_schedule_cancelled") == 1

        repeated_cancel = cancel_publication_schedule(actor=actor, publication_id=draft.id)
        assert repeated_cancel == cancelled
        assert _audit_count(actor.business_id, draft.id, "publication_schedule_cancelled") == 1


    def test_native_event_retry_cannot_overwrite_newer_reschedule(self) -> None:
        actor = _actor(840709, "Native retry")
        draft = create_publication_draft(
            actor=actor,
            title="Очередность",
            body="Текст",
            channel="vk",
        )
        now = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)

        first = schedule_publication(
            actor=actor,
            publication_id=draft.id,
            local_time="29.08.2026 12:00",
            now=now,
            idempotency_key="route:vk:event:A:member:840709:action:schedule",
        )
        newer = schedule_publication(
            actor=actor,
            publication_id=draft.id,
            local_time="30.08.2026 15:30",
            now=now,
            idempotency_key="route:vk:event:B:member:840709:action:schedule",
        )
        replay = schedule_publication(
            actor=actor,
            publication_id=draft.id,
            local_time="29.08.2026 12:00",
            now=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
            idempotency_key="route:vk:event:A:member:840709:action:schedule",
        )

        self.assertEqual(first.scheduled_at, replay.scheduled_at)
        self.assertEqual(first.updated_at, replay.updated_at)
        with get_db_ro() as conn:
            current = conn.execute(
                "SELECT status, scheduled_at FROM business_publications WHERE id=? AND business_id=?",
                (draft.id, actor.business_id),
            ).fetchone()
        self.assertEqual("scheduled", current["status"])
        self.assertEqual(newer.scheduled_at, current["scheduled_at"])
        self.assertEqual(
            2,
            _audit_count(
                actor.business_id,
                draft.id,
                "publication_schedule_mutation",
            ),
        )

        with self.assertRaisesRegex(ValueError, r"idempotency key"):
            schedule_publication(
                actor=actor,
                publication_id=draft.id,
                local_time="31.08.2026 10:00",
                now=now,
                idempotency_key="route:vk:event:A:member:840709:action:schedule",
            )


    def test_noop_native_event_retry_cannot_revert_newer_schedule(self) -> None:
        actor = _actor(840710, "Native noop retry")
        draft = create_publication_draft(
            actor=actor,
            title="No-op",
            body="Текст",
            channel="max",
        )
        now = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
        original = schedule_publication(
            actor=actor,
            publication_id=draft.id,
            local_time="29.08.2026 12:00",
            now=now,
        )
        no_op = schedule_publication(
            actor=actor,
            publication_id=draft.id,
            local_time="29.08.2026 12:00",
            now=now,
            idempotency_key="route:max:event:NOOP:member:840710:action:schedule",
        )
        self.assertEqual(original, no_op)

        newer = schedule_publication(
            actor=actor,
            publication_id=draft.id,
            local_time="30.08.2026 15:30",
            now=now,
            idempotency_key="route:max:event:NEW:member:840710:action:schedule",
        )
        replay = schedule_publication(
            actor=actor,
            publication_id=draft.id,
            local_time="29.08.2026 12:00",
            now=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
            idempotency_key="route:max:event:NOOP:member:840710:action:schedule",
        )
        self.assertEqual(original.scheduled_at, replay.scheduled_at)
        self.assertEqual(original.updated_at, replay.updated_at)
        with get_db_ro() as conn:
            current = conn.execute(
                "SELECT scheduled_at FROM business_publications WHERE id=? AND business_id=?",
                (draft.id, actor.business_id),
            ).fetchone()
        self.assertEqual(newer.scheduled_at, current["scheduled_at"])


    def test_scheduling_never_creates_delivery_work(self) -> None:
        actor = _actor(840702, "Без автодоставки")
        draft = create_publication_draft(actor=actor, title="Без отправки", body="Текст")
        schedule_publication(
            actor=actor,
            publication_id=draft.id,
            local_time="29.08.2026 13:00",
            now=datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc),
        )
        cancel_publication_schedule(actor=actor, publication_id=draft.id)

        with get_db_ro() as conn:
            delivery = conn.execute("SELECT COUNT(*) AS c FROM delivery_dispatch_outbox").fetchone()
            provider = conn.execute("SELECT COUNT(*) AS c FROM provider_dispatch_outbox").fetchone()
        assert int(delivery["c"]) == 0
        assert int(provider["c"]) == 0


    def test_scheduling_is_tenant_and_role_scoped(self) -> None:
        actor = _actor(840703, "Свой контент")
        other = _actor(840704, "Чужой контент")
        draft = create_publication_draft(actor=actor, title="Свой", body="Текст")
        now = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)

        with self.assertRaises(ValueError):
            schedule_publication(
                actor=other,
                publication_id=draft.id,
                local_time="29.08.2026 12:00",
                now=now,
            )

        member = grant_business_member(
            actor=actor,
            user_id=840705,
            role=PlatformRole.ANALYST,
        )
        analyst = resolve_tenant_context(user_id=member.user_id, business_id=actor.business_id)
        with self.assertRaises(TenantPermissionDenied):
            schedule_publication(
                actor=analyst,
                publication_id=draft.id,
                local_time="29.08.2026 12:00",
                now=now,
            )


    def test_scheduling_fails_closed_for_invalid_time_and_state(self) -> None:
        actor = _actor(840706, "Fail closed")
        now = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
        draft = create_publication_draft(actor=actor, title="Будущее", body="Текст")

        with self.assertRaisesRegex(ValueError, r"future"):
            schedule_publication(
                actor=actor,
                publication_id=draft.id,
                local_time="27.08.2026 12:00",
                now=now,
            )
        with self.assertRaisesRegex(ValueError, r"look like"):
            schedule_publication(
                actor=actor,
                publication_id=draft.id,
                local_time="завтра вечером",
                now=now,
            )
        with self.assertRaisesRegex(ValueError, r"only a scheduled"):
            cancel_publication_schedule(actor=actor, publication_id=draft.id)

        published = publish_publication(actor=actor, publication_id=draft.id)
        assert published.status == "published"
        with self.assertRaisesRegex(ValueError, r"not available"):
            schedule_publication(
                actor=actor,
                publication_id=draft.id,
                local_time="29.08.2026 12:00",
                now=now,
            )


    def test_scheduling_rejects_dst_gap_and_ambiguous_wall_clock(self) -> None:
        actor = _actor(840707, "DST", timezone_name="Europe/Berlin")
        gap = create_publication_draft(actor=actor, title="Gap", body="Текст")
        with self.assertRaisesRegex(ValueError, r"does not exist locally"):
            schedule_publication(
                actor=actor,
                publication_id=gap.id,
                local_time="29.03.2026 02:30",
                now=datetime(2026, 3, 28, 8, 0, tzinfo=timezone.utc),
            )

        ambiguous = create_publication_draft(actor=actor, title="Fold", body="Текст")
        with self.assertRaisesRegex(ValueError, r"ambiguous"):
            schedule_publication(
                actor=actor,
                publication_id=ambiguous.id,
                local_time="25.10.2026 02:30",
                now=datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc),
            )


    def test_scheduling_requires_valid_business_timezone(self) -> None:
        actor = _actor(840708, "Плохая зона")
        draft = create_publication_draft(actor=actor, title="Зона", body="Текст")
        with get_db() as conn:
            conn.execute(
                "UPDATE business_profiles SET timezone=? WHERE business_id=?",
                ("Invalid/Timezone", actor.business_id),
            )
        with self.assertRaisesRegex(ValueError, r"timezone is invalid"):
            schedule_publication(
                actor=actor,
                publication_id=draft.id,
                local_time="29.08.2026 12:00",
                now=datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc),
            )
