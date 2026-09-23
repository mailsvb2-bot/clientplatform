from __future__ import annotations

from types import SimpleNamespace

from clientplatform.application import native_member_interactions as native
from clientplatform.domain.customer_interactions import CustomerInteractionButton, CustomerInteractionMessage
from clientplatform.presentation import owner_navigation as nav
from handlers import clientplatform_ad_connections as ads
from handlers import clientplatform_admin as admin
from handlers import clientplatform_creative_studio as creative
from handlers import clientplatform_one_click_experience as one_click


def _flatten(markup) -> dict[str, str | None]:
    return {
        button.text: button.callback_data
        for row in markup.inline_keyboard
        for button in row
    }


def test_notice_main_menu_label_is_explicit_without_renaming_existing_home() -> None:
    assert nav.BACK.label == "⬅️ Назад"
    assert nav.HOME.label == "🏠 Главная"
    assert nav.MAIN_MENU_LABEL == "🏠 В главное меню"


def test_ad_notice_navigation_has_back_and_main_menu() -> None:
    rows = ads._owner_navigation_rows(
        "business-token",
        back_callback="cpa:home:business-token",
    )
    assert rows == [
        [(nav.BACK.label, "cpa:home:business-token")],
        [(nav.MAIN_MENU_LABEL, "cpj:home:business-token")],
    ]


def test_creative_studio_menu_and_result_keep_two_escape_routes() -> None:
    for markup in (
        creative._menu_rows("business-token"),
        creative._result_rows("business-token"),
    ):
        buttons = _flatten(markup)
        assert buttons[nav.BACK.label] == "cpo:content:business-token"
        assert buttons[nav.MAIN_MENU_LABEL] == "cpj:home:business-token"


def test_one_click_notice_navigation_has_parent_and_home() -> None:
    rows = one_click._notice_navigation_rows(
        "business-token",
        back_callback="cpa:home:business-token",
    )
    assert rows == [
        [(nav.BACK.label, "cpa:home:business-token")],
        [(nav.MAIN_MENU_LABEL, "cpj:home:business-token")],
    ]


def test_admin_back_keyboard_keeps_back_and_adds_main_menu() -> None:
    markup = admin._back_keyboard(SimpleNamespace(business_token="business-token"))
    buttons = _flatten(markup)
    assert buttons[nav.BACK.label] == "cpa:business-token:back"
    assert buttons[nav.HOME.label] == "cpa:business-token:leave"


def test_native_direct_child_has_explicit_back_and_main_menu() -> None:
    message = CustomerInteractionMessage(
        text="screen",
        rows=((CustomerInteractionButton(label="Действие", command="cpm:work"),),),
    )
    normalized = native._with_parent_navigation(
        message,
        native.ParsedMemberInteraction("menu-all"),
    )
    labels = [button.label for row in normalized.rows for button in row]
    commands = [button.command for row in normalized.rows for button in row]

    assert labels[-2:] == [nav.BACK.label, nav.MAIN_MENU_LABEL]
    assert commands[-2:] == ["cpm:menu", "cpm:menu"]
