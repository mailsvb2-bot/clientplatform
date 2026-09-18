from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.application import native_member_interactions as native
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import PlatformRole, TenantContext


EVENT_ID = "33333333-3333-4333-8333-333333333333"


class EventAdvertisingLinkParityTests(unittest.TestCase):
    def test_native_event_announcement_exposes_current_channel_and_ads_links(self) -> None:
        actor = TenantContext(
            business_id="11111111-1111-4111-8111-111111111111",
            user_id=101,
            membership_id="22222222-2222-4222-8222-222222222222",
            role=PlatformRole.OWNER,
        )
        draft = SimpleNamespace(
            text="Анонс вебинара",
            generated_by="ai:test",
            registration_url=lambda *, public_base_url, source: (
                f"{public_base_url}/e/demo?source={source}"
            ),
        )
        with (
            patch.object(native, "draft_event_announcement_template", return_value=draft),
            patch.object(
                native.settings,
                "MESSENGER_PUBLIC_BASE_URL",
                "https://clientplatform.example.test",
                create=True,
            ),
        ):
            for platform in (ConnectionPlatform.VK, ConnectionPlatform.MAX):
                with self.subTest(platform=platform.value):
                    message = native._event_announcement_message(
                        actor,
                        EVENT_ID,
                        current_platform=platform,
                    )
                    self.assertIn(
                        f"Регистрация: https://clientplatform.example.test/e/demo?source={platform.value}",
                        message.text,
                    )
                    self.assertIn("требует Вашего подтверждения", message.text)
                    self.assertIn("🔗 Ссылка для рекламы:", message.text)
                    self.assertIn(
                        "https://clientplatform.example.test/e/demo?source=ads",
                        message.text,
                    )


if __name__ == "__main__":
    unittest.main()
