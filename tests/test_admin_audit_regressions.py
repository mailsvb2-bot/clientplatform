from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import admin as admin_handler
from handlers import admin_inline
from handlers import menu as menu_handler


@pytest.mark.asyncio
async def test_admin_grant_failure_never_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_inline, "is_superadmin", lambda _uid: True)
    monkeypatch.setattr(
        admin_inline,
        "_grant_admin_role_sync",
        lambda _target_id: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        text="42",
        user_shared=None,
        forward_from=None,
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())

    await admin_inline.admin_add_admin_input(message, state)

    bodies = [str(call.args[0]) for call in message.answer.await_args_list]
    assert any("Не удалось добавить администратора" in body for body in bodies)
    assert all("✅ Добавил администратора" not in body for body in bodies)
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_gate_blocks_stale_callback_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = SimpleNamespace(
        roles={"marketing"},
        staff_kb=SimpleNamespace(inline_keyboard=[]),
        is_superadmin=False,
        allowed_perms={"admin:funnel"},
    )
    monkeypatch.setattr(admin_inline, "_load_admin_ctx", lambda _uid: ctx)

    async def no_back(_cb, _state):
        return False

    answer = AsyncMock()
    monkeypatch.setattr(admin_inline, "admin_nav_back", no_back)
    monkeypatch.setattr(admin_inline, "safe_answer_callback", answer)
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=7),
        data="admin:money:payment:42",
    )

    await admin_inline.admin_gate(callback, SimpleNamespace())

    answer.assert_awaited_once()
    assert answer.await_args.kwargs["show_alert"] is True
    assert "отозван" in answer.await_args.args[1]


@pytest.mark.asyncio
async def test_admin_grant_success_is_verified_before_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_inline, "is_superadmin", lambda _uid: True)
    monkeypatch.setattr(admin_inline, "_grant_admin_role_sync", lambda _target_id: None)
    monkeypatch.setattr(admin_inline, "get_staff_roles", lambda _target_id: {"admin"})

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        text="42",
        user_shared=None,
        forward_from=None,
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())

    await admin_inline.admin_add_admin_input(message, state)

    bodies = [str(call.args[0]) for call in message.answer.await_args_list]
    assert any("✅ Добавил администратора: 42" in body for body in bodies)
    state.clear.assert_awaited_once()


def test_delegated_staff_gets_panel_button_without_mutating_cached_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    base = SimpleNamespace(
        inline_keyboard=[[SimpleNamespace(callback_data="demo", text="demo")]]
    )
    monkeypatch.setattr(menu_handler, "kb_main", lambda user_id: base)
    monkeypatch.setattr(menu_handler, "is_staff", lambda user_id: True)

    result = menu_handler._main_menu_keyboard(42)

    assert result is not base
    assert any(
        getattr(button, "callback_data", None) == "admin:menu"
        for row in result.inline_keyboard
        for button in row
    )
    assert len(base.inline_keyboard) == 1


@pytest.mark.asyncio
async def test_admin_command_opens_panel_for_delegated_staff(monkeypatch: pytest.MonkeyPatch) -> None:
    keyboard = SimpleNamespace(inline_keyboard=[])
    monkeypatch.setattr(admin_handler, "is_staff", lambda _uid: True)
    monkeypatch.setattr(
        admin_inline,
        "_load_admin_ctx",
        lambda _uid: SimpleNamespace(staff_kb=keyboard),
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=77),
        answer=AsyncMock(),
    )

    await admin_handler.admin_cmd(message)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.kwargs["reply_markup"] is keyboard
