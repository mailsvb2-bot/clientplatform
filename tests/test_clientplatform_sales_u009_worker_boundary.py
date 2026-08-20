from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone

import unittest
from unittest import mock

from clientplatform.application import dispatch_worker
from clientplatform.domain.connections import ConnectionPlatform, DispatchStatus
from clientplatform.domain.programs import ContentKind
from clientplatform.infrastructure.unified_dispatch_outbox import (
    ClaimedProviderDispatch,
    ProviderDispatch,
)
from clientplatform.transport.base import AdapterRegistry


def _claimed_followup() -> ClaimedProviderDispatch:
    stamp = "2026-08-20T10:00:00+00:00"
    dispatch = ProviderDispatch(
        id="00000000-0000-4000-8000-000000000001",
        business_id="00000000-0000-4000-8000-000000000002",
        platform=ConnectionPlatform.TELEGRAM,
        source_kind="sales_followup",
        source_id="00000000-0000-4000-8000-000000000003",
        connection_id="00000000-0000-4000-8000-000000000004",
        external_subject="700001",
        payload_kind=ContentKind.TEXT,
        payload_ref="follow up",
        idempotency_key="sales-followup:test",
        status=DispatchStatus.SENDING,
        attempts=0,
        available_at=stamp,
        created_at=stamp,
        updated_at=stamp,
        locked_at=stamp,
        lock_token="lease-token",
    )
    return ClaimedProviderDispatch(
        dispatch=dispatch,
        external_subject="700001",
        credential_reference="secret://u009/test",
    )


class _Repository:
    def __init__(self, _conn: object) -> None:
        pass

    def claim_due(self, **_kwargs: object) -> list[ClaimedProviderDispatch]:
        return [_claimed_followup()]


class _Credentials:
    calls = 0

    def resolve(self, _reference: str) -> str:
        self.calls += 1
        raise AssertionError("credential resolution must not happen after boundary rejection")


class _Adapter:
    platform = ConnectionPlatform.TELEGRAM
    calls = 0

    async def send(self, _item: object, _credential: str) -> str:
        self.calls += 1
        raise AssertionError("provider send must not happen after boundary rejection")


class SalesFollowupWorkerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_boundary_rejection_precedes_credentials_and_provider(self) -> None:
        credentials = _Credentials()
        adapter = _Adapter()
        with (
            mock.patch.object(dispatch_worker, "get_db", lambda: nullcontext(object())),
            mock.patch.object(dispatch_worker, "DispatchOutboxRepository", _Repository),
            mock.patch.object(
                dispatch_worker,
                "_provider_claim_can_cross_provider_boundary",
                lambda _item: False,
            ),
        ):
            result = await dispatch_worker.run_dispatch_batch(
                credential_provider=credentials,
                adapters=AdapterRegistry([adapter]),
                limit=1,
                max_attempts=3,
                lock_ttl_seconds=30,
            )

        self.assertEqual(result.claimed, 1)
        self.assertEqual(result.sent, 0)
        self.assertEqual(result.retried, 0)
        self.assertEqual(result.dead, 0)
        self.assertEqual(credentials.calls, 0)
        self.assertEqual(adapter.calls, 0)


if __name__ == "__main__":
    unittest.main()
