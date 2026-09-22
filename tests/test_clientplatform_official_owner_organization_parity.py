from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

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


def test_business_selector_exposes_create_organization() -> None:
    first = str(uuid4())
    second = str(uuid4())

    rendered = _interaction(
        entry._business_selector_reply(
            [_access(first, "Первая организация"), _access(second, "Вторая организация")]
        )
    )

    buttons = _buttons(rendered)
    assert buttons["Первая организация"].startswith("cpw:open:")
    assert buttons["Вторая организация"].startswith("cpw:open:")
    assert buttons["➕ Создать организацию"] == "business"
    assert "создать ещё одну организацию" in rendered.text.casefold()


@pytest.mark.parametrize("platform", ["vk", "max"])
def test_official_owner_settings_expose_create_organization(platform: str) -> None:
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
        patch.object(entry, "render_native_member_interaction", return_value=base),
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

    assert reply is not None
    rendered = _interaction(reply)
    buttons = _buttons(rendered)
    assert buttons["🧭 Направления деятельности"] == "cpm:directions:0"
    assert buttons["➕ Создать организацию"] == "business"
    assert buttons["⬅️ Назад"] == "cpm:menu-all"
    assert "создать ещё одну организацию" in rendered.text.casefold()


@pytest.mark.parametrize("platform", ["vk", "max"])
def test_official_non_owner_settings_do_not_expose_create_organization(platform: str) -> None:
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
        patch.object(entry, "render_native_member_interaction", return_value=base),
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

    assert reply is not None
    assert "➕ Создать организацию" not in _buttons(_interaction(reply))


def test_create_organization_button_uses_existing_channel_neutral_onboarding() -> None:
    parsed = entry.parse_clientplatform_entry_command("business")

    assert parsed is not None
    assert parsed.action == "create_business"
