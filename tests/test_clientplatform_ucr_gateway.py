from __future__ import annotations

import unittest
from typing import Any, Mapping

from clientplatform.runtime.secrets import SecretReferenceError
from clientplatform.runtime.ucr_gateway import (
    UCR_PINNED_REVISION,
    UcrGatewayClient,
    UcrGatewayConfig,
    UcrGatewayConfigurationError,
    UcrGatewayProtocolError,
    UcrGatewayRejected,
    UcrGatewayUnavailable,
    ucr_gateway_config,
)


class _CredentialProvider:
    def __init__(self, token: str = "t" * 48, *, unavailable: bool = False) -> None:
        self.token = token
        self.unavailable = unavailable
        self.references: list[str] = []

    def resolve(self, reference: str) -> str:
        self.references.append(reference)
        if self.unavailable:
            raise SecretReferenceError("unavailable")
        return self.token


class _Transport:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes | None = None,
    ) -> None:
        self.status = status
        self.body = body or (
            '{"ok":true,"ucr_revision":"' + UCR_PINNED_REVISION + '"}'
        ).encode("utf-8")
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> tuple[int, Mapping[str, str], bytes]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "payload": None if payload is None else dict(payload),
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        return self.status, {"Content-Type": "application/json"}, self.body


def _config(**overrides: Any) -> UcrGatewayConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "base_url": "https://ucr-gateway.example.test/root/",
        "token_reference": "secret://env/CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN",
    }
    values.update(overrides)
    return UcrGatewayConfig(**values)


class ClientPlatformUcrGatewayConfigTests(unittest.TestCase):
    def test_disabled_default_does_not_require_a_url(self) -> None:
        config = ucr_gateway_config({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.base_url, "")
        self.assertEqual(config.pinned_revision, UCR_PINNED_REVISION)
        self.assertEqual(
            config.token_reference,
            "secret://env/CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN",
        )

    def test_enabled_gateway_requires_an_explicit_url(self) -> None:
        with self.assertRaisesRegex(UcrGatewayConfigurationError, "URL is required"):
            ucr_gateway_config({"CLIENTPLATFORM_UCR_GATEWAY_ENABLED": "true"})

    def test_external_plain_http_is_rejected_but_loopback_is_allowed(self) -> None:
        with self.assertRaisesRegex(UcrGatewayConfigurationError, "requires HTTPS"):
            _config(base_url="http://ucr.example.test")
        local = _config(base_url="http://127.0.0.1:8911/")
        self.assertEqual(local.base_url, "http://127.0.0.1:8911")

    def test_url_credentials_query_and_fragment_are_rejected(self) -> None:
        for value in (
            "https://user:pass@ucr.example.test",
            "https://ucr.example.test?token=bad",
            "https://ucr.example.test/#secret",
        ):
            with self.subTest(value=value):
                with self.assertRaises(UcrGatewayConfigurationError):
                    _config(base_url=value)

    def test_raw_token_cannot_be_used_as_a_reference(self) -> None:
        with self.assertRaisesRegex(UcrGatewayConfigurationError, "secret reference"):
            _config(token_reference="this-is-a-raw-token-and-must-never-live-in-config")

    def test_revision_cannot_drift_from_repository_pin(self) -> None:
        with self.assertRaisesRegex(UcrGatewayConfigurationError, "repository pin"):
            _config(pinned_revision="main")


class ClientPlatformUcrGatewayClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_uses_secret_reference_and_revision_header(self) -> None:
        transport = _Transport()
        credentials = _CredentialProvider()
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=credentials,
            transport=transport,
        )

        response = await client.health()

        self.assertTrue(response["ok"])
        self.assertEqual(
            credentials.references,
            ["secret://env/CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN"],
        )
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(
            call["url"],
            "https://ucr-gateway.example.test/root/v1/health",
        )
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + "t" * 48)
        self.assertEqual(
            call["headers"]["X-ClientPlatform-UCR-Revision"],
            UCR_PINNED_REVISION,
        )
        self.assertNotIn("Idempotency-Key", call["headers"])

    async def test_context_bootstrap_is_bounded_and_idempotent(self) -> None:
        transport = _Transport()
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=transport,
        )

        await client.ensure_communication_context(
            tenant_key="business:11111111-1111-1111-1111-111111111111",
            identity_key="customer:22222222-2222-2222-2222-222222222222",
            device_key="clientplatform:service-device",
            group_key="conversation:33333333-3333-3333-3333-333333333333",
            member_identity_keys=("customer:a", "staff:b"),
            idempotency_key="context:33333333-3333-3333-3333-333333333333",
        )

        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["Idempotency-Key"], "context:33333333-3333-3333-3333-333333333333")
        self.assertEqual(call["payload"]["version"], 1)
        self.assertEqual(call["payload"]["member_identity_keys"], ["customer:a", "staff:b"])
        self.assertNotIn("token", call["payload"])

    async def test_context_rejects_duplicate_members_before_network(self) -> None:
        transport = _Transport()
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=transport,
        )
        with self.assertRaisesRegex(ValueError, "must be unique"):
            await client.ensure_communication_context(
                tenant_key="business:a",
                identity_key="customer:a",
                device_key="device:a",
                group_key="conversation:a",
                member_identity_keys=("customer:a", "customer:a"),
                idempotency_key="context:abcdefgh",
            )
        self.assertEqual(transport.calls, [])

    async def test_call_requires_two_unique_participants(self) -> None:
        transport = _Transport()
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=transport,
        )
        with self.assertRaisesRegex(ValueError, "at least two"):
            await client.start_call(
                tenant_key="business:a",
                context_key="conversation:a",
                participant_identity_keys=("customer:a",),
                idempotency_key="call:abcdefgh",
            )
        with self.assertRaisesRegex(ValueError, "must be unique"):
            await client.start_call(
                tenant_key="business:a",
                context_key="conversation:a",
                participant_identity_keys=("customer:a", "customer:a"),
                idempotency_key="call:abcdefgh",
            )
        self.assertEqual(transport.calls, [])

    async def test_missing_or_short_credential_fails_closed(self) -> None:
        for provider in (
            _CredentialProvider(unavailable=True),
            _CredentialProvider(token="short"),
        ):
            with self.subTest(provider=provider):
                client = UcrGatewayClient(
                    config=_config(),
                    credential_provider=provider,
                    transport=_Transport(),
                )
                with self.assertRaisesRegex(UcrGatewayUnavailable, "credential is unavailable"):
                    await client.health()

    async def test_revision_mismatch_is_rejected(self) -> None:
        transport = _Transport(body=b'{"ok":true,"ucr_revision":"different"}')
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=transport,
        )
        with self.assertRaisesRegex(UcrGatewayProtocolError, "revision_mismatch"):
            await client.health()

    async def test_gateway_auth_rejection_does_not_echo_response_text(self) -> None:
        transport = _Transport(
            status=401,
            body=b'{"error":"authentication_failed","detail":"secret-value"}',
        )
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=transport,
        )
        with self.assertRaisesRegex(UcrGatewayRejected, "authentication_failed") as caught:
            await client.health()
        self.assertNotIn("secret-value", str(caught.exception))

    async def test_malformed_json_fails_closed(self) -> None:
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=_Transport(body=b"not-json"),
        )
        with self.assertRaisesRegex(UcrGatewayProtocolError, "invalid JSON"):
            await client.health()

    async def test_disabled_gateway_never_resolves_secret_or_uses_network(self) -> None:
        credentials = _CredentialProvider()
        transport = _Transport()
        client = UcrGatewayClient(
            config=_config(enabled=False),
            credential_provider=credentials,
            transport=transport,
        )
        with self.assertRaisesRegex(UcrGatewayConfigurationError, "disabled"):
            await client.health()
        self.assertEqual(credentials.references, [])
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
