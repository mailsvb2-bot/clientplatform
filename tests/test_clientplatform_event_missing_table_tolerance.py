from __future__ import annotations

import sqlite3
from typing import Any

from clientplatform.application import event_warmups
from clientplatform.application.event_delivery_targets import (
    resolve_event_registration_messenger_target,
)


class _MissingTableConnection:
    """Connection whose first read fails the way a not-yet-migrated table does.

    services.db.core normalizes Postgres "does not exist"/"undefined table"
    errors into sqlite3.OperationalError, so both engines reach these guards.
    """

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError(
            "no such table: clientplatform_event_registration_channels"
        )


def test_messenger_target_tolerates_missing_registration_channels_table() -> None:
    assert (
        resolve_event_registration_messenger_target(
            _MissingTableConnection(),
            business_id="business-1",
            event_id="event-1",
            registration_id="registration-1",
            platform="telegram",
        )
        is None
    )


def test_warmup_visual_asset_tolerates_missing_content_asset_table() -> None:
    assert (
        event_warmups._active_visual_asset(
            _MissingTableConnection(),
            business_id="business-1",
            event_id="event-1",
            slot_key="warmup-1",
        )
        is None
    )
