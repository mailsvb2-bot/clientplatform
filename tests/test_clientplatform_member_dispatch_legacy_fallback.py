from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

from clientplatform.infrastructure.safe_member_dispatch_outbox import (
    DispatchOutboxRepository,
)
from clientplatform.infrastructure.safe_unified_dispatch_outbox import (
    DispatchOutboxRepository as UnifiedDispatchOutboxRepository,
)


def test_member_wrapper_preserves_legacy_claim_path_without_provider_table() -> None:
    conn = sqlite3.connect(":memory:")
    repository = DispatchOutboxRepository(conn)
    current = datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc)
    with (
        patch.object(repository, "_provider_table_available", return_value=False),
        patch.object(
            repository,
            "_quarantine_stale_member_interaction_boundaries",
            side_effect=AssertionError("quarantine must not query an absent provider table"),
        ),
        patch.object(
            UnifiedDispatchOutboxRepository,
            "claim_due",
            return_value=["legacy-lesson-claim"],
        ) as parent_claim,
    ):
        claimed = repository.claim_due(limit=3, lock_ttl_seconds=90, now=current)

    assert claimed == ["legacy-lesson-claim"]
    parent_claim.assert_called_once_with(
        limit=3,
        lock_ttl_seconds=90,
        now=current,
    )
    conn.close()
