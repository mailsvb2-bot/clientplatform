from __future__ import annotations

from uuid import uuid4

from clientplatform.application import native_member_interactions as native
from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.presentation import owner_navigation as nav
from handlers import clientplatform_growth as growth
from handlers import clientplatform_sales as sales


def _actor() -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        membership_id=str(uuid4()),
        user_id=101,
        role=PlatformRole.OWNER,
    )


def _button_map(markup) -> dict[str, str | None]:
    return {
        button.text: button.callback_data
        for row in markup.inline_keyboard
        for button in row
    }


def test_native_owner_menu_hierarchy_has_explicit_parent_routes() -> None:
    expected = {
        "menu-all": "cpm:menu",
        "work": "cpm:menu-all",
        "work-more": "cpm:work",
        "today": "cpm:work",
        "customers": "cpm:work",
        "bookings": "cpm:work",
        "booking-open": "cpm:bookings",
        "programs": "cpm:work",
        "growth": "cpm:menu-all",
        "growth-sales": "cpm:growth",
        "growth-analysis": "cpm:growth-sales",
        "growth-more": "cpm:growth",
        "growth-lifecycle": "cpm:growth-more",
        "manage": "cpm:menu-all",
        "manage-more": "cpm:manage",
        "team": "cpm:menu-all",
        "members": "cpm:team",
        "messengers": "cpm:menu-all",
    }
    for action, parent in expected.items():
        parsed = native.ParsedMemberInteraction(action)
        assert native._native_parent_command(parsed) == parent


def test_native_navigation_normalizer_replaces_home_only_with_back_and_home() -> None:
    actor = _actor()
    raw = native._growth_sales_message(actor)
    normalized = native._with_parent_navigation(
        raw,
        native.ParsedMemberInteraction("growth-sales"),
    )

    commands = [button.command for row in normalized.rows for button in row]
    labels = [button.label for row in normalized.rows for button in row]
    assert normalized.rows[-1][0].label == nav.BACK.label
    assert normalized.rows[-1][0].command == "cpm:growth"
    assert nav.HOME.label in labels
    assert "cpm:menu" in commands
    assert sum(len(row) for row in normalized.rows) <= 10


def test_native_root_screen_does_not_get_a_back_button() -> None:
    message = CustomerInteractionMessage(
        text="root",
        rows=((CustomerInteractionButton(label="Действие", command="cpm:work"),),),
    )
    normalized = native._with_parent_navigation(
        message,
        native.ParsedMemberInteraction("menu"),
    )
    assert normalized == message
    assert all(
        button.label != nav.BACK.label
        for row in normalized.rows
        for button in row
    )


def test_native_pagination_label_is_not_confused_with_hierarchy_back() -> None:
    customers = [
        type("Customer", (), {"id": str(uuid4()), "display_name": f"Клиент {i}"})()
        for i in range(15)
    ]
    actor = _actor()
    original = native.list_customers
    try:
        native.list_customers = lambda **_kwargs: customers
        page = native._customers_message(actor, 1)
    finally:
        native.list_customers = original

    normalized = native._with_parent_navigation(
        page,
        native.ParsedMemberInteraction("customers", ("1",)),
    )
    labels = [button.label for row in normalized.rows for button in row]
    assert "◀️ Предыдущая страница" in labels
    assert normalized.rows[-1][0].label == nav.BACK.label
    assert normalized.rows[-1][0].command == "cpm:work"


def test_telegram_growth_and_sales_section_menus_have_back_and_home() -> None:
    business_id = str(uuid4())
    growth_buttons = _button_map(
        growth._cockpit_keyboard(
            business_id=business_id,
            period_days=7,
            action_key="none",
        )
    )
    sales_buttons = _button_map(sales._home_keyboard(business_id))

    for buttons in (growth_buttons, sales_buttons):
        assert nav.BACK.label in buttons
        assert nav.HOME.label in buttons

    token = growth.uuid_token(business_id)
    assert growth_buttons[nav.BACK.label] == f"cpo:more:{token}"
    assert sales_buttons[nav.BACK.label] == f"cpo:clients:{token}"
