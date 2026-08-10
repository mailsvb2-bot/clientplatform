from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import admin_inline
from handlers import admin_inline_resources as resources
from services.platform_resource_limits import PlatformResourceSnapshot, ResourceCounter
from services.roles import ROLE_ADMIN


def _snapshot() -> PlatformResourceSnapshot:
    return PlatformResourceSnapshot(
        configured=True,
        telemetry_available=True,
        base_url="http://visual-creative-gateway:8097",
        token_configured=True,
        day_utc="2026-08-11",
        resets_at="2026-08-12T00:00:00Z",
        usage_semantics="gateway_reservations_not_provider_billing",
        jobs=ResourceCounter(21, 30, 9),
        image=ResourceCounter(21, 30, 9),
        video=ResourceCounter(0, 5, 5),
        active=ResourceCounter(1, 3, 2),
    )


def test_resource_handler_ignores_unrelated_callback():
    result = asyncio.run(
        resources.handle(
            SimpleNamespace(),
            object(),
            "admin:other",
            SimpleNamespace(is_superadmin=True),
        )
    )
    assert result is False


def test_resource_handler_denies_non_superadmin(monkeypatch):
    answers: list[tuple[str, bool]] = []

    async def fake_answer(_cb, text: str, *, show_alert: bool = False):
        answers.append((text, show_alert))

    monkeypatch.setattr(resources, "safe_answer_callback", fake_answer)
    result = asyncio.run(
        resources.handle(
            SimpleNamespace(),
            object(),
            "admin:resources",
            SimpleNamespace(is_superadmin=False),
        )
    )
    assert result is True
    assert answers == [("Только для супер-админа.", True)]


def test_resource_handler_renders_snapshot_and_monitor_state(monkeypatch):
    rendered: list[dict[str, object]] = []
    monkeypatch.setattr(resources, "get_platform_resource_snapshot", _snapshot)
    monkeypatch.setattr(
        resources,
        "platform_resource_monitor_snapshot",
        lambda: {
            "running": True,
            "last_tick_age_sec": 12.4,
            "last_error": "",
        },
    )

    async def fake_edit(_cb, _state, text, reply_markup=None, *, push=True, reset_stack=False):
        rendered.append(
            {
                "text": text,
                "reply_markup": reply_markup,
                "push": push,
                "reset_stack": reset_stack,
            }
        )

    monkeypatch.setattr(resources, "safe_edit_admin", fake_edit)
    ctx = SimpleNamespace(is_superadmin=True)

    assert asyncio.run(
        resources.handle(SimpleNamespace(), object(), "admin:resources", ctx)
    ) is True
    assert "21/30 (70%)" in str(rendered[-1]["text"])
    assert "Автонапоминания супер-админу: ✅ работает" in str(rendered[-1]["text"])
    assert "12 сек. назад" in str(rendered[-1]["text"])
    assert rendered[-1]["push"] is True
    keyboard = rendered[-1]["reply_markup"]
    assert isinstance(keyboard, InlineKeyboardMarkup)
    assert keyboard.inline_keyboard[0][0].callback_data == "admin:resources:refresh"

    assert asyncio.run(
        resources.handle(SimpleNamespace(), object(), "admin:resources:refresh", ctx)
    ) is True
    assert rendered[-1]["push"] is False


def test_monitor_text_surfaces_stopped_and_error(monkeypatch):
    monkeypatch.setattr(
        resources,
        "platform_resource_monitor_snapshot",
        lambda: {
            "running": False,
            "last_tick_age_sec": None,
            "last_error": "visual_gateway_http_404",
        },
    )
    text = resources._monitor_text()
    assert "⚠️ не запущен" in text
    assert "visual_gateway_http_404" in text


def test_superadmin_menu_injects_platform_resource_button(monkeypatch):
    monkeypatch.setattr(admin_inline, "is_admin", lambda _uid: True)
    monkeypatch.setattr(admin_inline, "is_superadmin", lambda _uid: True)
    monkeypatch.setattr(admin_inline, "get_staff_roles", lambda _uid: {ROLE_ADMIN})

    def fake_staff_menu(*_args, **_kwargs):
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Existing", callback_data="admin:system:checks")],
                [InlineKeyboardButton(text="Back", callback_data="menu:main")],
            ]
        )

    monkeypatch.setattr(admin_inline, "kb_staff_menu", fake_staff_menu)
    ctx = admin_inline._load_admin_ctx(123)
    assert ctx is not None
    callbacks = [
        button.callback_data
        for row in ctx.staff_kb.inline_keyboard
        for button in row
    ]
    assert callbacks.count("admin:resources") == 1
    assert callbacks[-1] == "menu:main"
