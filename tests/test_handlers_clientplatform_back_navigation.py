from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from handlers import clientplatform_admin as admin


@dataclass
class FakeState:
    data: dict[str, Any] = field(default_factory=dict)

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history", "current", "expected_call", "expected_history", "expected_section"),
    [
        (["menu"], "today", "menu", [], "menu"),
        (["menu"], "today-full", "menu", [], "menu"),
        (["menu"], "behavior", "menu", [], "menu"),
        (["menu"], "messengers", "menu", [], "menu"),
        (["menu"], "attention", "menu", [], "menu"),
        (["menu"], "autopilot", "menu", [], "menu"),
        (["menu"], "release", "menu", [], "menu"),
        (["menu"], "tariff", "menu", [], "menu"),
        (["menu", "customer-list"], "customer", "customer-list", ["menu"], "customer-list"),
        (["menu", "customers"], "customer", "customers", ["menu"], "customers"),
        (["menu", "members"], "member", "members", ["menu"], "members"),
        (["menu", "add-member"], "add-role", "add-member", ["menu"], "add-member"),
        ([], "unknown", "menu", [], "menu"),
    ],
)
async def test_back_returns_to_the_expected_parent_screen(
    monkeypatch: pytest.MonkeyPatch,
    history: list[str],
    current: str,
    expected_call: str,
    expected_history: list[str],
    expected_section: str,
) -> None:
    calls: list[str] = []

    async def render_menu(*_args: Any, **_kwargs: Any) -> None:
        calls.append("menu")

    async def render_customer_list(
        *_args: Any,
        today_only: bool,
        **_kwargs: Any,
    ) -> None:
        calls.append("customers" if today_only else "customer-list")

    async def render_members(*_args: Any, **_kwargs: Any) -> None:
        calls.append("members")

    async def begin_add_member(*_args: Any, **_kwargs: Any) -> None:
        calls.append("add-member")

    monkeypatch.setattr(admin, "_render_menu", render_menu)
    monkeypatch.setattr(admin, "_render_customer_list", render_customer_list)
    monkeypatch.setattr(admin, "_render_members", render_members)
    monkeypatch.setattr(admin, "_begin_add_member", begin_add_member)

    state = FakeState(
        {
            "cp_admin_history": list(history),
            "cp_admin_section": current,
        }
    )

    await admin._navigate_back(
        SimpleNamespace(),
        state,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert calls == [expected_call]
    assert state.data["cp_admin_history"] == expected_history
    assert state.data["cp_admin_section"] == expected_section


def test_all_section_back_buttons_use_the_back_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = "00000000-0000-0000-0000-000000000001"
    monkeypatch.setattr(admin.control, "_uuid_token", lambda _value: "business-token")
    monkeypatch.setattr(admin.control, "_token_uuid", lambda _value: business_id)
    ctx = SimpleNamespace(business_token="business-token")

    markup = admin._back_keyboard(ctx)  # type: ignore[arg-type]
    button = markup.inline_keyboard[-1][0]

    assert button.text == "⬅️ Назад"
    assert button.callback_data is not None
    assert admin._parse_callback(button.callback_data) == (business_id, "back", ())


def test_main_admin_back_button_leaves_to_the_business_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    business_id = "00000000-0000-0000-0000-000000000001"
    monkeypatch.setattr(admin.control, "_uuid_token", lambda _value: "business-token")
    monkeypatch.setattr(admin.control, "_token_uuid", lambda _value: business_id)
    ctx = SimpleNamespace(
        role=admin.PlatformRole.OWNER,
        business_token="business-token",
    )

    markup = admin._menu_keyboard(ctx)  # type: ignore[arg-type]
    button = markup.inline_keyboard[-1][0]

    assert button.text == "⬅️ Назад"
    assert button.callback_data is not None
    assert admin._parse_callback(button.callback_data) == (business_id, "leave", ())
