from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from runtime import health_server


def _snapshot(**overrides):
    value = {
        'clientplatform_dispatch_configured': False,
        'clientplatform_runtime_health_available': True,
        'clientplatform_runtime_composed': False,
        'clientplatform_dispatch_enabled': False,
        'clientplatform_dispatch_running': False,
        'clientplatform_dispatch_iterations': 0,
        'clientplatform_dispatch_claimed': 0,
        'clientplatform_dispatch_sent': 0,
        'clientplatform_dispatch_retried': 0,
        'clientplatform_dispatch_dead': 0,
        'clientplatform_dispatch_errors': 0,
        'clientplatform_dispatch_last_error': '',
        'clientplatform_dispatch_last_tick_age_seconds': 0,
    }
    value.update(overrides)
    return value


class ClientPlatformHealthPayloadIntegrationTests(unittest.TestCase):
    def _patch_common_payload_dependencies(self, stack: ExitStack) -> None:
        stack.enter_context(
            patch.object(
                health_server,
                'CONFIG',
                SimpleNamespace(engine='sqlite', uses_postgres=False),
            )
        )
        stack.enter_context(patch.object(health_server, 'redacted_db_target', return_value='redacted'))
        stack.enter_context(patch.object(health_server, '_telegram_transport', return_value='polling'))
        stack.enter_context(
            patch.object(health_server, '_messenger_webhook_configured', return_value=False)
        )
        stack.enter_context(patch.object(health_server, '_webhook_configured', return_value=False))
        stack.enter_context(
            patch.object(
                health_server,
                '_messenger_preflight_readiness',
                return_value=(True, [], {}),
            )
        )
        stack.enter_context(patch.object(health_server, '_ingress_health_fields', return_value={}))
        stack.enter_context(patch.object(health_server, '_storage_health_fields', return_value={}))
        stack.enter_context(patch.object(health_server, 'ai_policy_snapshot', return_value={}))

    def test_health_payload_exposes_clientplatform_runtime_diagnostics(self) -> None:
        clientplatform = _snapshot(
            clientplatform_dispatch_configured=True,
            clientplatform_runtime_composed=True,
            clientplatform_dispatch_enabled=True,
            clientplatform_dispatch_running=True,
            clientplatform_dispatch_iterations=2,
            clientplatform_dispatch_sent=7,
        )
        with ExitStack() as stack:
            self._patch_common_payload_dependencies(stack)
            stack.enter_context(patch.object(health_server, 'clientplatform_runtime_snapshot', return_value=clientplatform))
            payload, status = health_server.build_health_payload()

        self.assertEqual(status, 200)
        self.assertTrue(payload['clientplatform_dispatch_configured'])
        self.assertTrue(payload['clientplatform_dispatch_running'])
        self.assertEqual(payload['clientplatform_dispatch_sent'], 7)

    def test_readiness_fails_when_enabled_clientplatform_scheduler_is_not_running(self) -> None:
        clientplatform = _snapshot(
            clientplatform_dispatch_configured=True,
            clientplatform_runtime_composed=True,
            clientplatform_dispatch_enabled=True,
            clientplatform_dispatch_running=False,
        )
        with ExitStack() as stack:
            self._patch_common_payload_dependencies(stack)
            stack.enter_context(patch.object(health_server, '_db_ready', return_value=(True, None)))
            stack.enter_context(patch.object(health_server, '_schema_ready', return_value=(True, None)))
            stack.enter_context(patch.object(health_server, 'clientplatform_runtime_snapshot', return_value=clientplatform))
            stack.enter_context(patch.object(health_server, 'http_ingress_enabled', return_value=False))
            stack.enter_context(patch.object(health_server, 'required_readiness_tables', return_value=[]))
            payload, status = health_server.build_readiness_payload()

        self.assertEqual(status, 500)
        self.assertFalse(payload['ok'])
        self.assertFalse(payload['clientplatform_dispatch_ready'])
        self.assertIn('clientplatform_dispatch:not_running', payload['error'])


if __name__ == '__main__':
    unittest.main()
