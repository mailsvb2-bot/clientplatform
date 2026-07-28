from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.clientplatform_ephemeral_media_gateway import ephemeral_gateway_config
from scripts.clientplatform_staging_fixture import fixture_bytes, write_fixture
from scripts.clientplatform_telegram_staging_smoke import (
    _extract_start_chat_ids,
    _resolve_chat_id,
)


class ClientPlatformStagingFixtureTests(unittest.TestCase):
    def test_fixture_is_small_valid_deterministic_mp3(self) -> None:
        data = fixture_bytes()
        self.assertTrue(data.startswith(b"ID3"))
        self.assertLess(len(data), 10_000)
        self.assertEqual(
            hashlib.sha256(data).hexdigest(),
            "bb380bc775c0708f6a567e24f85ba639e955e5e61ef745820baa010557f51d60",
        )

    def test_fixture_writer_creates_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = write_fixture(Path(temp) / "bucket" / "fixture.mp3")
            self.assertEqual(target.read_bytes(), fixture_bytes())


class ClientPlatformEphemeralGatewayConfigTests(unittest.TestCase):
    def test_gateway_is_loopback_filesystem_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(
                os.environ,
                {
                    "CLIENTPLATFORM_MEDIA_GATEWAY_FILESYSTEM_ROOT": temp,
                    "CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS": "clientplatform-staging",
                    "CLIENTPLATFORM_MEDIA_GATEWAY_PORT": "8091",
                },
                clear=False,
            ):
                config = ephemeral_gateway_config()
        self.assertTrue(config.enabled)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.storage_mode, "filesystem")
        self.assertEqual(config.allowed_buckets, frozenset({"clientplatform-staging"}))
        self.assertEqual(config.route_prefix, "/clientplatform")
        self.assertFalse(config.s3_endpoint)


class ClientPlatformTelegramChatDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_extracts_only_private_start_messages(self) -> None:
        updates = [
            {
                "message": {
                    "text": "/start",
                    "chat": {"id": 101, "type": "private"},
                }
            },
            {
                "message": {
                    "text": "/start@clientplatform_staging_bot payload",
                    "chat": {"id": "101", "type": "private"},
                }
            },
            {
                "message": {
                    "text": "hello",
                    "chat": {"id": 202, "type": "private"},
                }
            },
            {
                "message": {
                    "text": "/start",
                    "chat": {"id": -303, "type": "group"},
                }
            },
        ]
        self.assertEqual(_extract_start_chat_ids(updates), ("101",))

    async def test_explicit_chat_id_avoids_provider_lookup(self) -> None:
        async def exploding_post(_url, _payload, _timeout):
            raise AssertionError("explicit chat id must not call Telegram")

        chat_id = await _resolve_chat_id(
            token="staging-token",
            telegram_base_url="https://api.telegram.org",
            explicit_chat_id="700001",
            post_json=exploding_post,
        )
        self.assertEqual(chat_id, "700001")

    async def test_discovers_one_private_start_chat(self) -> None:
        methods: list[str] = []

        async def post_json(url, _payload, _timeout):
            method = url.rsplit("/", 1)[-1]
            methods.append(method)
            if method == "getWebhookInfo":
                return 200, {"ok": True, "result": {"url": ""}}
            return 200, {
                "ok": True,
                "result": [
                    {
                        "message": {
                            "text": "/start",
                            "chat": {"id": 700001, "type": "private"},
                        }
                    }
                ],
            }

        chat_id = await _resolve_chat_id(
            token="staging-token",
            telegram_base_url="https://api.telegram.org",
            post_json=post_json,
        )
        self.assertEqual(chat_id, "700001")
        self.assertEqual(methods, ["getWebhookInfo", "getUpdates"])

    async def test_active_webhook_fails_closed(self) -> None:
        async def post_json(_url, _payload, _timeout):
            return 200, {
                "ok": True,
                "result": {"url": "https://webhook.example.test/clientplatform"},
            }

        with self.assertRaisesRegex(RuntimeError, "webhook_active"):
            await _resolve_chat_id(
                token="staging-token",
                telegram_base_url="https://api.telegram.org",
                post_json=post_json,
            )

    async def test_multiple_start_chats_require_explicit_id(self) -> None:
        async def post_json(url, _payload, _timeout):
            if url.endswith("/getWebhookInfo"):
                return 200, {"ok": True, "result": {"url": ""}}
            return 200, {
                "ok": True,
                "result": [
                    {
                        "message": {
                            "text": "/start",
                            "chat": {"id": 1, "type": "private"},
                        }
                    },
                    {
                        "message": {
                            "text": "/start",
                            "chat": {"id": 2, "type": "private"},
                        }
                    },
                ],
            }

        with self.assertRaisesRegex(RuntimeError, "chat_ambiguous"):
            await _resolve_chat_id(
                token="staging-token",
                telegram_base_url="https://api.telegram.org",
                post_json=post_json,
            )


class ClientPlatformStagingWorkflowContractTests(unittest.TestCase):
    def test_workflow_has_pinned_ephemeral_tunnel_and_one_required_secret(self) -> None:
        workflow = Path(".github/workflows/clientplatform-telegram-staging.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("cloudflared/releases/download/2026.7.3", workflow)
        self.assertIn(
            "9d71c677db00134c1bd4144b7783486b654ad281b1ea62b4972098d19f770f17",
            workflow,
        )
        self.assertIn(r"trycloudflare\.com", workflow)
        self.assertIn("scripts/clientplatform_ephemeral_media_gateway.py", workflow)
        self.assertIn("scripts/clientplatform_staging_fixture.py", workflow)
        self.assertIn("secrets.CLIENTPLATFORM_STAGING_TELEGRAM_BOT_TOKEN", workflow)
        self.assertNotIn("secrets.CLIENTPLATFORM_MEDIA_SIGNING_KEY", workflow)
        self.assertNotIn("vars.CLIENTPLATFORM_MEDIA_GATEWAY_BASE_URL", workflow)
        self.assertNotIn("vars.CLIENTPLATFORM_STAGING_MEDIA_REFERENCE", workflow)


if __name__ == "__main__":
    unittest.main()
