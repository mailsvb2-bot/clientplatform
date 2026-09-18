from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from clientplatform.application.event_owner_flow import (
    MultiSessionOnlineEventCreateRequest,
    OnlineEventSessionCreateRequest,
    append_multisession_online_event_draft_session_in_transaction,
    create_multisession_online_event_draft_in_transaction,
    publish_multisession_online_event_draft_in_transaction,
)
from clientplatform.infrastructure.event_repository import EventRepository, EventStateConflict
from clientplatform.infrastructure.event_session_repository import EventSessionRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db.schema import create_or_update_tables


class EventDraftWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        create_or_update_tables(self.conn)
        tenancy = TenancyRepository(self.conn)
        access = tenancy.create_business(owner_user_id=101, name="Практика")
        self.actor = tenancy.resolve_context(
            user_id=101,
            business_id=access.business.id,
        )
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def tearDown(self) -> None:
        self.conn.close()

    def _session(self, days: int, room: str) -> OnlineEventSessionCreateRequest:
        starts_at = self.now + timedelta(days=days)
        return OnlineEventSessionCreateRequest(
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            join_url=room,
        )

    def test_draft_accumulates_sessions_and_publishes_only_at_finish(self) -> None:
        first = self._session(2, "https://example.test/day-1")
        second = self._session(3, "https://example.test/day-2")
        draft = create_multisession_online_event_draft_in_transaction(
            self.conn,
            actor=self.actor,
            request=MultiSessionOnlineEventCreateRequest(
                title="Трёхдневный интенсив",
                timezone_name="Europe/Moscow",
                sessions=(first,),
                enable_email_notifications=False,
            ),
        )
        event = EventRepository(self.conn).get(actor=self.actor, event_id=draft.event_id)
        self.assertEqual(event.status, "draft")
        self.assertEqual(
            len(EventSessionRepository(self.conn).list_for_event(actor=self.actor, event_id=event.id)),
            1,
        )

        configured = append_multisession_online_event_draft_session_in_transaction(
            self.conn,
            actor=self.actor,
            event_id=event.id,
            session=second,
        )
        self.assertEqual([item.position for item in configured], [1, 2])
        self.assertEqual(configured[1].join_url, "https://example.test/day-2")
        refreshed = EventRepository(self.conn).get(actor=self.actor, event_id=event.id)
        self.assertEqual(refreshed.status, "draft")
        self.assertEqual(refreshed.ends_at, configured[-1].ends_at)

        published = publish_multisession_online_event_draft_in_transaction(
            self.conn,
            actor=self.actor,
            event_id=event.id,
        )
        self.assertTrue(published.join_ready)
        final = EventRepository(self.conn).get(actor=self.actor, event_id=event.id)
        self.assertEqual(final.status, "published")

        with self.assertRaisesRegex(EventStateConflict, "only draft"):
            append_multisession_online_event_draft_session_in_transaction(
                self.conn,
                actor=self.actor,
                event_id=event.id,
                session=self._session(4, "https://example.test/day-3"),
            )


if __name__ == "__main__":
    unittest.main()
