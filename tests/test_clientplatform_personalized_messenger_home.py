from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.customer_interactions import CustomerInteractionMessage
from clientplatform.domain.tenancy import PlatformRole
from services.messenger.clientplatform_entry import handle_clientplatform_entry

import handlers.clientplatform_simple_experience as simple


class PersonalizedMessengerHomeTests(unittest.TestCase):
    def test_telegram_uses_business_aware_quick_actions_and_mini_app_escape(self) -> None:
        business_id = str(uuid4())
        capabilities = [
            SimpleNamespace(
                connector_key="consultations",
                status=CapabilityStatus.ACTIVE,
            ),
            SimpleNamespace(
                connector_key="programs",
                status=CapabilityStatus.ACTIVE,
            ),
        ]
        with patch.object(
            simple,
            "cockpit_web_app_url",
            return_value="https://app.clientplatform.test/clientplatform/cockpit",
        ):
            markup = simple._simple_keyboard(
                business_id,
                activity_description=(
                    "Я психолог. Провожу консультации, вебинары и обучающие программы."
                ),
                capabilities=capabilities,
                role=PlatformRole.OWNER,
            )

        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(
            labels,
            [
                "💬 Клиенты и обращения",
                "📅 Записать на консультацию",
                "🎥 Провести вебинар",
                "🎓 Материалы и программы",
                "👥 Найти клиентов",
                "📊 Результаты",
                "▦ Все возможности",
            ],
        )
        last = markup.inline_keyboard[-1][0]
        self.assertIsNone(last.callback_data)
        self.assertEqual(
            last.web_app.url,
            "https://app.clientplatform.test/clientplatform/cockpit",
        )

    def test_vk_and_max_use_the_same_business_aware_quick_actions(self) -> None:
        entry = SimpleNamespace(user_id=102)
        access = SimpleNamespace(
            business=SimpleNamespace(id="business-102", name="Практика Марии")
        )
        actor = SimpleNamespace(
            user_id=102,
            business_id="business-102",
            role=PlatformRole.OWNER,
        )
        profile = SimpleNamespace(
            activity_description=(
                "Я психолог. Провожу консультации, вебинары и обучающие программы."
            )
        )
        capabilities = [
            SimpleNamespace(connector_key="consultations", status="active"),
            SimpleNamespace(connector_key="programs", status="active"),
        ]
        with (
            patch(
                "services.messenger.clientplatform_entry.register_user_entry",
                return_value=entry,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_accessible_businesses",
                return_value=[access],
            ),
            patch(
                "services.messenger.clientplatform_entry.resolve_tenant_context",
                return_value=actor,
            ),
            patch(
                "services.messenger.clientplatform_entry.get_business_profile",
                return_value=profile,
            ),
            patch(
                "services.messenger.clientplatform_entry.list_business_capabilities",
                return_value=capabilities,
            ),
            patch(
                "services.messenger.clientplatform_entry.render_native_member_interaction",
            ) as render,
        ):
            for platform in ("vk", "max"):
                with self.subTest(platform=platform):
                    _, replies = handle_clientplatform_entry(
                        102,
                        platform=platform,
                        external_user_id=f"{platform}-102",
                        text="/start",
                    )
                    restored = CustomerInteractionMessage.from_json(
                        replies[0].meta["interaction"]
                    )
                    self.assertEqual(
                        [row[0].label for row in restored.rows],
                        [
                            "💬 Клиенты и обращения",
                            "📅 Записать на консультацию",
                            "🎥 Провести вебинар",
                            "🎓 Материалы и программы",
                            "👥 Найти клиентов",
                            "📊 Результаты",
                            "▦ Все возможности",
                        ],
                    )
                    self.assertEqual(restored.rows[-1][0].command, "cpm:menu-all")
        render.assert_not_called()


if __name__ == "__main__":
    unittest.main()
