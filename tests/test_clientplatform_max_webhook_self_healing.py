from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from clientplatform.runtime import native_messenger_reconciliation as reconciliation
from clientplatform.runtime.native_messenger_reconciliation import (
    MaxWebhookReconcileCandidate,
)
from clientplatform.runtime.secrets import SecretReferenceError


class _Credentials:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def resolve(self, reference: str) -> str:
        try:
            return self.values[reference]
        except KeyError:
            raise SecretReferenceError("unavailable") from None


class _Sender:
    def __init__(self, token: str, calls: list[tuple[str, str, str]]) -> None:
        self.token = token
        self.calls = calls

    async def ensure_webhook_subscription(self, *, url: str, secret: str):
        self.calls.append((self.token, url, secret))
        return {"success": True}


class MaxWebhookSelfHealingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconciles_existing_canonical_route_without_new_state(self) -> None:
        candidate = MaxWebhookReconcileCandidate(
            route_id="00000000-0000-4000-8000-000000000010",
            business_id="00000000-0000-4000-8000-000000000020",
            provider_token_reference="vault://connection/token-ref",
            webhook_secret_reference="vault://connection/secret-ref",
        )
        calls: list[tuple[str, str, str]] = []
        with patch.object(
            reconciliation,
            "_list_active_max_candidates",
            return_value=(candidate,),
        ):
            result = await reconciliation.reconcile_max_webhook_batch(
                public_base_url="https://client.example.test",
                credential_provider=_Credentials(
                    {
                        candidate.provider_token_reference: "provider-token",
                        candidate.webhook_secret_reference: "webhook-secret",
                    }
                ),
                sender_factory=lambda token: _Sender(token, calls),  # type: ignore[arg-type]
                request_delay_seconds=0,
            )

        self.assertEqual((result.scanned, result.reconciled, result.failed), (1, 1, 0))
        self.assertIsNone(result.next_cursor)
        self.assertEqual(
            calls,
            [
                (
                    "provider-token",
                    "https://client.example.test/clientplatform/webhooks/max/"
                    "00000000-0000-4000-8000-000000000010",
                    "webhook-secret",
                )
            ],
        )

    async def test_one_broken_route_does_not_block_other_businesses(self) -> None:
        first = MaxWebhookReconcileCandidate(
            route_id="00000000-0000-4000-8000-000000000010",
            business_id="00000000-0000-4000-8000-000000000020",
            provider_token_reference="vault://connection/missing-token",
            webhook_secret_reference="vault://connection/secret-one",
        )
        second = MaxWebhookReconcileCandidate(
            route_id="00000000-0000-4000-8000-000000000011",
            business_id="00000000-0000-4000-8000-000000000021",
            provider_token_reference="vault://connection/token-two",
            webhook_secret_reference="vault://connection/secret-two",
        )
        calls: list[tuple[str, str, str]] = []
        with patch.object(
            reconciliation,
            "_list_active_max_candidates",
            return_value=(first, second),
        ):
            result = await reconciliation.reconcile_max_webhook_batch(
                public_base_url="https://client.example.test",
                credential_provider=_Credentials(
                    {
                        second.provider_token_reference: "provider-two",
                        second.webhook_secret_reference: "secret-two",
                    }
                ),
                sender_factory=lambda token: _Sender(token, calls),  # type: ignore[arg-type]
                request_delay_seconds=0,
            )

        self.assertEqual((result.scanned, result.reconciled, result.failed), (2, 1, 1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "provider-two")

    async def test_full_batch_returns_cursor_for_bounded_followup(self) -> None:
        candidates = tuple(
            MaxWebhookReconcileCandidate(
                route_id=f"00000000-0000-4000-8000-{index:012d}",
                business_id="00000000-0000-4000-8000-000000000020",
                provider_token_reference=f"vault://connection/token-{index}",
                webhook_secret_reference=f"vault://connection/secret-{index}",
            )
            for index in range(2)
        )
        sender = AsyncMock()
        sender.ensure_webhook_subscription.return_value = {"success": True}
        values = {
            reference: "value"
            for candidate in candidates
            for reference in (
                candidate.provider_token_reference,
                candidate.webhook_secret_reference,
            )
        }
        with patch.object(
            reconciliation,
            "_list_active_max_candidates",
            return_value=candidates,
        ) as listing:
            result = await reconciliation.reconcile_max_webhook_batch(
                public_base_url="https://client.example.test",
                limit=2,
                credential_provider=_Credentials(values),
                sender_factory=lambda _token: sender,
                request_delay_seconds=0,
            )

        listing.assert_called_once_with(cursor=None, limit=2)
        self.assertEqual(result.next_cursor, candidates[-1].route_id)
        self.assertEqual(result.reconciled, 2)

    async def test_public_origin_rejects_non_https_or_explicit_port(self) -> None:
        for base in (
            "http://client.example.test",
            "https://client.example.test:8443",
            "https://client.example.test/path",
        ):
            with self.subTest(base=base), self.assertRaises(ValueError):
                await reconciliation.reconcile_max_webhook_batch(
                    public_base_url=base,
                    request_delay_seconds=0,
                )


if __name__ == "__main__":
    unittest.main()
