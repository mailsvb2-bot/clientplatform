from __future__ import annotations

import base64
import json
import unittest

from ucr_gateway_sidecar.server import (
    GatewayConfigurationError,
    GatewayService,
    GatewayUpstreamError,
    SidecarConfig,
    UCR_INTEGRATION_SERVICE,
    UCR_PINNED_REVISION,
    load_config,
)


class FakeBackend:
    def __init__(self) -> None:
        self.health_calls = 0
        self.calls: list[dict[str, object]] = []
        self.health_error: GatewayUpstreamError | None = None
        self.invoke_error: GatewayUpstreamError | None = None

    async def health(self) -> None:
        self.health_calls += 1
        if self.health_error:
            raise self.health_error

    async def invoke(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        if self.invoke_error:
            raise self.invoke_error
        return {"identity": {"identityId": {"value": "identity-a"}}}


def config(**overrides: object) -> SidecarConfig:
    values: dict[str, object] = {
        "client_token": "t" * 32,
        "grpc_target": "127.0.0.1:50051",
        "grpc_tls_enabled": False,
        "service_credential_id": b"credential-a",
        "service_credential_secret": b"s" * 32,
    }
    values.update(overrides)
    return SidecarConfig(**values)  # type: ignore[arg-type]


def headers(**extra: str) -> dict[str, str]:
    value = {
        "Authorization": "Bearer " + ("t" * 32),
        "X-ClientPlatform-UCR-Revision": UCR_PINNED_REVISION,
    }
    value.update(extra)
    return value


class SidecarTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_authenticates_before_upstream(self) -> None:
        backend = FakeBackend()
        service = GatewayService(config=config(), backend=backend)
        status, payload = await service.health(headers={})
        self.assertEqual(status, 409)
        self.assertEqual(backend.health_calls, 0)
        self.assertEqual(payload["error"], "ucr_gateway_revision_mismatch")

    async def test_health_success(self) -> None:
        backend = FakeBackend()
        service = GatewayService(config=config(), backend=backend)
        status, payload = await service.health(headers=headers())
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "ucr_revision": UCR_PINNED_REVISION})
        self.assertEqual(backend.health_calls, 1)

    async def test_mutation_requires_idempotency_and_forwards_exact_request(self) -> None:
        backend = FakeBackend()
        service = GatewayService(config=config(), backend=backend)
        body = json.dumps(
            {
                "version": 1,
                "service": UCR_INTEGRATION_SERVICE,
                "method": "CreateIdentity",
                "request": {"identity": {"scope": {"tenantId": {"value": "t1"}}}},
            }
        ).encode()
        status, payload = await service.rpc(headers=headers(), body=body)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "ucr_gateway_idempotency_key_invalid")
        self.assertEqual(backend.calls, [])

        status, payload = await service.rpc(
            headers=headers(**{"Idempotency-Key": "request-0001"}), body=body
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], UCR_INTEGRATION_SERVICE)
        self.assertEqual(payload["method"], "CreateIdentity")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(
            backend.calls[0]["request"],
            {"identity": {"scope": {"tenantId": {"value": "t1"}}}},
        )
        self.assertEqual(backend.calls[0]["idempotency_key"], "request-0001")

    async def test_read_rejects_idempotency_key(self) -> None:
        backend = FakeBackend()
        service = GatewayService(config=config(), backend=backend)
        body = json.dumps(
            {
                "version": 1,
                "service": UCR_INTEGRATION_SERVICE,
                "method": "GetIdentity",
                "request": {
                    "scope": {"tenantId": {"value": "t1"}},
                    "identityId": {"value": "i1"},
                },
            }
        ).encode()
        status, payload = await service.rpc(
            headers=headers(**{"Idempotency-Key": "request-0001"}), body=body
        )
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "ucr_gateway_read_idempotency_forbidden")
        self.assertEqual(backend.calls, [])

    async def test_unknown_method_rejected_before_backend(self) -> None:
        backend = FakeBackend()
        service = GatewayService(config=config(), backend=backend)
        body = json.dumps(
            {
                "version": 1,
                "service": UCR_INTEGRATION_SERVICE,
                "method": "RegisterDevice",
                "request": {"device": {}},
            }
        ).encode()
        status, payload = await service.rpc(headers=headers(), body=body)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "ucr_rpc_method_not_allowed")
        self.assertEqual(backend.calls, [])

    async def test_nonfinite_json_rejected(self) -> None:
        backend = FakeBackend()
        service = GatewayService(config=config(), backend=backend)
        body = (
            b'{"version":1,"service":"ucr.v1.IntegrationService",'
            b'"method":"GetIdentity","request":{"value":NaN}}'
        )
        status, payload = await service.rpc(headers=headers(), body=body)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "ucr_gateway_request_nonfinite")
        self.assertEqual(backend.calls, [])

    async def test_upstream_error_is_sanitized(self) -> None:
        backend = FakeBackend()
        backend.invoke_error = GatewayUpstreamError(503, "ucr_grpc_unavailable")
        service = GatewayService(config=config(), backend=backend)
        body = json.dumps(
            {
                "version": 1,
                "service": UCR_INTEGRATION_SERVICE,
                "method": "GetIdentity",
                "request": {
                    "scope": {"tenantId": {"value": "t1"}},
                    "identityId": {"value": "i1"},
                },
            }
        ).encode()
        status, payload = await service.rpc(headers=headers(), body=body)
        self.assertEqual(status, 503)
        self.assertEqual(payload, {"ok": False, "error": "ucr_grpc_unavailable"})


class ConfigTests(unittest.TestCase):
    def test_external_plaintext_grpc_is_rejected(self) -> None:
        with self.assertRaises(GatewayConfigurationError):
            config(grpc_target="ucr.example:443", grpc_tls_enabled=False)

    def test_secret_debug_is_redacted(self) -> None:
        value = repr(config())
        self.assertNotIn("t" * 32, value)
        self.assertNotIn("s" * 32, value)

    def test_env_requires_exact_32_byte_ucr_secret(self) -> None:
        env = {
            "CLIENTPLATFORM_SECRET_UCR_GATEWAY_TOKEN": "t" * 32,
            "CLIENTPLATFORM_UCR_GRPC_TARGET": "127.0.0.1:50051",
            "CLIENTPLATFORM_UCR_GRPC_TLS_ENABLED": "0",
            "CLIENTPLATFORM_UCR_SERVICE_CREDENTIAL_ID": "credential-a",
            "CLIENTPLATFORM_SECRET_UCR_SERVICE_CREDENTIAL_B64": base64.b64encode(
                b"s" * 32
            ).decode(),
        }
        loaded = load_config(env)
        self.assertEqual(loaded.service_credential_secret, b"s" * 32)
        broken = dict(env)
        broken["CLIENTPLATFORM_SECRET_UCR_SERVICE_CREDENTIAL_B64"] = base64.b64encode(
            b"s" * 31
        ).decode()
        with self.assertRaises(GatewayConfigurationError):
            load_config(broken)


if __name__ == "__main__":
    unittest.main()
