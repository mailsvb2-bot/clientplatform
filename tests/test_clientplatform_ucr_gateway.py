from __future__ import annotations

import json
import unittest
from typing import Any, Mapping

from clientplatform.runtime.secrets import SecretReferenceError
from clientplatform.runtime.ucr_gateway import (
    UCR_CALL_SERVICE,
    UCR_INTEGRATION_SERVICE,
    UCR_PINNED_REVISION,
    UcrCallMethod,
    UcrGatewayClient,
    UcrGatewayConfig,
    UcrGatewayConfigurationError,
    UcrGatewayProtocolError,
    UcrGatewayRejected,
    UcrGatewayUnavailable,
    UcrIntegrationMethod,
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


def _rpc_body(
    *,
    service: str,
    method: str,
    result: Mapping[str, Any] | None = None,
) -> bytes:
    return json.dumps(
        {
            "ok": True,
            "ucr_revision": UCR_PINNED_REVISION,
            "service": service,
            "method": method,
            "result": dict(result or {"accepted": True}),
        }
    ).encode("utf-8")


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

    def test_public_rpc_whitelists_match_pinned_ucr_proto(self) -> None:
        self.assertEqual(
            {method.value for method in UcrIntegrationMethod},
            {
                "SubmitCommand",
                "CreateIdentity",
                "LinkIdentity",
                "GetIdentity",
                "ResolveIdentityBinding",
                "CreateConversation",
                "GetConversation",
                "SendMessage",
                "GetMessage",
                "CreateCommunicationIntent",
                "GetCommunicationIntent",
            },
        )
        self.assertEqual(
            {method.value for method in UcrCallMethod},
            {"StartCall", "GetCall", "SignalCall"},
        )


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

    async def test_integration_mutation_is_exact_rpc_and_idempotent(self) -> None:
        request = {
            "conversation": {
                "scope": {"tenant_id": {"value": "tenant-a"}},
                "conversation_id": {"value": "conversation-a"},
            }
        }
        transport = _Transport(
            body=_rpc_body(
                service=UCR_INTEGRATION_SERVICE,
                method="CreateConversation",
                result=request,
            )
        )
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=transport,
        )

        response = await client.invoke_integration(
            method=UcrIntegrationMethod.CREATE_CONVERSATION,
            request=request,
            idempotency_key="conversation:abcdefgh",
        )

        self.assertTrue(response["ok"])
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://ucr-gateway.example.test/root/v1/rpc")
        self.assertEqual(call["headers"]["Idempotency-Key"], "conversation:abcdefgh")
        self.assertEqual(
            call["payload"],
            {
                "version": 1,
                "service": UCR_INTEGRATION_SERVICE,
                "method": "CreateConversation",
                "request": request,
            },
        )

    async def test_integration_read_needs_no_idempotency_key(self) -> None:
        transport = _Transport(
            body=_rpc_body(
                service=UCR_INTEGRATION_SERVICE,
                method="GetConversation",
            )
        )
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=transport,
        )

        await client.invoke_integration(
            method=UcrIntegrationMethod.GET_CONVERSATION,
            request={
                "scope": {"tenant_id": {"value": "tenant-a"}},
                "conversation_id": {"value": "conversation-a"},
            },
        )

        self.assertNotIn("Idempotency-Key", transport.calls[0]["headers"])

    async def test_call_request_is_forwarded_without_inventing_call_state(self) -> None:
        request = {
            "call": {
                "scope": {"tenant_id": {"value": "tenant-a"}},
                "call_id": {"value": "call-a"},
                "conversation": {"conversation_id": {"value": "conversation-a"}},
                "initiated_by": {"principal_id": {"value": "principal-a"}},
                "participants": [
                    {"principal_id": {"value": "principal-a"}},
                    {"principal_id": {"value": "principal-b"}},
                ],
            }
        }
        transport = _Transport(
            body=_rpc_body(service=UCR_CALL_SERVICE, method="StartCall", result=request)
        )
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=transport,
        )

        await client.invoke_call(
            method=UcrCallMethod.START_CALL,
            request=request,
            idempotency_key="call:abcdefgh",
        )

        self.assertEqual(transport.calls[0]["payload"]["request"], request)
        self.assertNotIn("tenant_key", transport.calls[0]["payload"])
        self.assertNotIn("participant_identity_keys", transport.calls[0]["payload"])

    async def test_mutation_without_idempotency_key_fails_before_network(self) -> None:
        transport = _Transport()
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=transport,
        )
        with self.assertRaisesRegex(ValueError, "idempotency key is invalid"):
            await client.invoke_integration(
                method=UcrIntegrationMethod.CREATE_IDENTITY,
                request={"identity": {"identity_id": {"value": "identity-a"}}},
            )
        self.assertEqual(transport.calls, [])

    async def test_read_rejects_idempotency_key_before_network(self) -> None:
        transport = _Transport()
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=transport,
        )
        with self.assertRaisesRegex(ValueError, "read-only UCR RPC"):
            await client.invoke_call(
                method=UcrCallMethod.GET_CALL,
                request={"call_id": {"value": "call-a"}},
                idempotency_key="callread:abcdefgh",
            )
        self.assertEqual(transport.calls, [])

    async def test_unknown_method_cannot_escape_public_rpc_whitelist(self) -> None:
        transport = _Transport()
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=transport,
        )
        with self.assertRaisesRegex(ValueError, "unsupported UCR method"):
            await client.invoke_integration(
                method="RegisterDevice",  # type: ignore[arg-type]
                request={"device": {"id": "device-a"}},
                idempotency_key="device:abcdefgh",
            )
        self.assertEqual(transport.calls, [])

    async def test_empty_or_nonfinite_request_fails_before_network(self) -> None:
        transport = _Transport()
        client = UcrGatewayClient(
            config=_config(),
            credential_provider=_CredentialProvider(),
            transport=transport,
        )
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            await client.invoke_integration(
                method=UcrIntegrationMethod.GET_IDENTITY,
                request={},
            )
        with self.assertRaisesRegex(ValueError, "valid finite JSON"):
            await client.invoke_integration(
                method=UcrIntegrationMethod.GET_IDENTITY,
                request={"score": float("nan")},
            )
        self.assertEqual(transport.calls, [])

    async def test_rpc_response_must_echo_exact_service_method_and_result(self) -> None:
        cases = (
            (
                _rpc_body(service=UCR_CALL_SERVICE, method="GetConversation"),
                "service_mismatch",
            ),
            (
                _rpc_body(service=UCR_INTEGRATION_SERVICE, method="GetIdentity"),
                "method_mismatch",
            ),
            (
                json.dumps(
                    {
                        "ok": True,
                        "ucr_revision": UCR_PINNED_REVISION,
                        "service": UCR_INTEGRATION_SERVICE,
                        "method": "GetConversation",
                    }
                ).encode("utf-8"),
                "result_missing",
            ),
        )
        for body, error in cases:
            with self.subTest(error=error):
                client = UcrGatewayClient(
                    config=_config(),
                    credential_provider=_CredentialProvider(),
                    transport=_Transport(body=body),
                )
                with self.assertRaisesRegex(UcrGatewayProtocolError, error):
                    await client.invoke_integration(
                        method=UcrIntegrationMethod.GET_CONVERSATION,
                        request={"conversation_id": {"value": "conversation-a"}},
                    )

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
                with self.assertRaisesRegex(
                    UcrGatewayUnavailable,
                    "credential is unavailable",
                ):
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
        with self.assertRaisesRegex(
            UcrGatewayRejected,
            "authentication_failed",
        ) as caught:
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