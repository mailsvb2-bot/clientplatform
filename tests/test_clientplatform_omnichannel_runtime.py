from __future__ import annotations

import importlib.util
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.messenger_channels import MessengerIngressRoute
from clientplatform.runtime.messenger_provider_clients import MaxRuntimeClient

_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None


class _FakeRequest:
    def __init__(self, payload: dict[str, object], *, route_id: str, headers: dict[str, str] | None = None) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.match_info = {"route_id": route_id}
        self.headers = headers or {}

    async def read(self) -> bytes:
        return self._raw


@unittest.skipUnless(_AIOHTTP_AVAILABLE, "aiohttp runtime dependency is not installed in dependency-light Canon")
class OmnichannelRuntimeIngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_vk_confirmation_returns_route_scoped_secret_before_customer_extraction(self) -> None:
        from clientplatform.runtime.messenger_channel_ingress import canonical_vk_webhook

        route = MessengerIngressRoute(
            id=str(uuid4()),
            business_id=str(uuid4()),
            connection_id=str(uuid4()),
            platform=ConnectionPlatform.VK,
            external_route_id="424242",
            webhook_secret_reference="secret://env/CLIENTPLATFORM_SECRET_VK_WEBHOOK_TEST",
            confirmation_code_reference="secret://env/CLIENTPLATFORM_SECRET_VK_CONFIRMATION_TEST",
            status="active",
            created_by_member_id=str(uuid4()),
            created_at="2026-08-16T00:00:00+00:00",
            updated_at="2026-08-16T00:00:00+00:00",
        )
        request = _FakeRequest(
            {
                "type": "confirmation",
                "group_id": "424242",
                "secret": "route-webhook-secret",
            },
            route_id=route.id,
        )

        def _resolve_secret(reference: str) -> str:
            if reference == route.webhook_secret_reference:
                return "route-webhook-secret"
            if reference == route.confirmation_code_reference:
                return "vk-confirmation-code"
            raise AssertionError(f"unexpected secret reference: {reference}")

        with (
            patch(
                "clientplatform.runtime.messenger_channel_ingress.resolve_messenger_ingress_route",
                return_value=route,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.EnvironmentCredentialProvider.resolve",
                side_effect=_resolve_secret,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress._vk_raw_message",
                side_effect=AssertionError("confirmation must not enter customer extraction"),
            ),
        ):
            response = await canonical_vk_webhook(request)  # type: ignore[arg-type]

        self.assertEqual(200, response.status)
        self.assertEqual("vk-confirmation-code", response.text)


class _FakeMaxSender:
    def __init__(self) -> None:
        self.legacy_ui: bool | None = None
        self.text: str | None = None

    async def send_text(self, external_subject: str, text: str, **kwargs):
        del external_subject
        self.text = text
        self.legacy_ui = kwargs.get("legacy_ui")
        return {"message_id": "max-message-1"}


class OmnichannelRuntimeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_max_text_explicitly_disables_legacy_metrotherapy_ui(self) -> None:
        sender = _FakeMaxSender()
        with patch(
            "clientplatform.runtime.messenger_provider_clients._max_sender",
            return_value=sender,
        ):
            message_id = await MaxRuntimeClient().send_text(
                token="provider-token",
                external_subject="max-user-1",
                text="1",
                idempotency_key="dispatch:max:1",
            )

        self.assertEqual("max-message-1", message_id)
        self.assertEqual("1", sender.text)
        self.assertIs(sender.legacy_ui, False)


if __name__ == "__main__":
    unittest.main()
