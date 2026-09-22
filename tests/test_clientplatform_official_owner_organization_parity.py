from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from services.messenger import clientplatform_entry as entry


def _access(business_id: str, name: str):
    return SimpleNamespace(business=SimpleNamespace(id=business_id, name=name))


def _interaction(reply) -> CustomerInteractionMessage:
    return CustomerInteractionMessage.from_json(reply.meta["interaction"])


def _buttons(message: CustomerInteractionMessage) -> dict[str, str]:
    return {
        button.label: button.command
        for row in message.rows
        for button in row
    }


class OfficialOwnerOrganizationParityTests(TestCase):
    def test_business_selector_exposes_create_organization(self) -> None:
        first = str(uuid4())
        second = str(uuid4())

        rendered = _interaction(
            entry._business_selector_reply(
                [_access(first, "Первая организация"), _access(second, "Вторая организация")]
            )
        )

        buttons = _buttons(rendered)
        self.assertTrue(buttons["Первая организация"].startswith("cpw:open:"))
        self.assertTrue(buttons["Вторая организация"].startswith("cpw:open:"))
        self.assertEqual(buttons["➕ Создать организацию"], "business")
        self.assertIn("создать ещё одну организацию", rendered.text.casefold())

    def test_business_selector_dense_middle_page_stays_within_button_contract(self) -> None:
        accesses = [
            _access(str(uuid4()), f"Организация {index}")
            for index in range(15)
        ]

        rendered = _interaction(entry._business_selector_reply(accesses, page=1))

        buttons = [button for row in rendered.rows for button in row]
        self.assertEqual(len(buttons), 10)
        commands = {button.command for button in buttons}
        self.assertIn("cpw:list:0", commands)
        self.assertIn("cpw:list:2", commands)
        self.assertIn("business", commands)

    def test_official_owner_settings_expose_create_organization_in_vk_and_max(self) -> None:
        for platform in ("vk", "max"):
            with self.subTest(platform=platform):
                business_id = str(uuid4())
                actor = TenantContext(
                    business_id=business_id,
                    membership_id=str(uuid4()),
                    user_id=101,
                    role=PlatformRole.OWNER,
                )
                base = CustomerInteractionMessage(
                    text="⚙️ Настроить бизнес",
                    rows=(
                        (
                            CustomerInteractionButton(
                                label="🧭 Направления деятельности",
                                command="cpm:directions:0",
                            ),
                        ),
                        (
                            CustomerInteractionButton(
                                label="⬅️ Назад",
                                command="cpm:menu-all",
                            ),
                        ),
                    ),
                )

                with (
                    patch.object(entry, "_business_actor", return_value=actor),
                    patch.object(
                        entry,
                        "render_native_member_interaction",
                        return_value=base,
                    ),
                    patch.object(entry, "NativeMessengerSetupLinkService") as setup_links,
                ):
                    setup_links.return_value.issue_command.return_value = "noop"
                    reply = entry._owner_control_reply(
                        canonical_user_id=actor.user_id,
                        platform=platform,
                        accesses=[_access(business_id, "Организация")],
                        raw_text="cpm:manage",
                        business_id=business_id,
                        interaction_key=f"{platform}-manage",
                    )

                self.assertIsNotNone(reply)
                assert reply is not None
                rendered = _interaction(reply)
                buttons = _buttons(rendered)
                self.assertEqual(
                    buttons["🧭 Направления деятельности"],
                    "cpm:directions:0",
                )
                self.assertEqual(buttons["➕ Создать организацию"], "business")
                self.assertEqual(buttons["⬅️ Назад"], "cpm:menu-all")
                self.assertIn("создать ещё одну организацию", rendered.text.casefold())

    def test_official_non_owner_settings_do_not_expose_create_organization(self) -> None:
        for platform in ("vk", "max"):
            with self.subTest(platform=platform):
                business_id = str(uuid4())
                actor = TenantContext(
                    business_id=business_id,
                    membership_id=str(uuid4()),
                    user_id=202,
                    role=PlatformRole.ADMINISTRATOR,
                )
                base = CustomerInteractionMessage(
                    text="⚙️ Настроить бизнес",
                    rows=(
                        (
                            CustomerInteractionButton(
                                label="🧭 Направления деятельности",
                                command="cpm:directions:0",
                            ),
                        ),
                    ),
                )

                with (
                    patch.object(entry, "_business_actor", return_value=actor),
                    patch.object(
                        entry,
                        "render_native_member_interaction",
                        return_value=base,
                    ),
                    patch.object(entry, "NativeMessengerSetupLinkService") as setup_links,
                ):
                    setup_links.return_value.issue_command.return_value = "noop"
                    reply = entry._owner_control_reply(
                        canonical_user_id=actor.user_id,
                        platform=platform,
                        accesses=[_access(business_id, "Организация")],
                        raw_text="cpm:manage",
                        business_id=business_id,
                        interaction_key=f"{platform}-manage",
                    )

                self.assertIsNotNone(reply)
                assert reply is not None
                self.assertNotIn(
                    "➕ Создать организацию",
                    _buttons(_interaction(reply)),
                )

    def test_create_organization_button_uses_existing_channel_neutral_onboarding(self) -> None:
        parsed = entry.parse_clientplatform_entry_command("business")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.action, "create_business")
