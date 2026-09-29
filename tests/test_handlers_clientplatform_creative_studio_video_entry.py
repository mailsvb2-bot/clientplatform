from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from clientplatform.domain.creative_generation import CreativeGenerationReceiptStatus
from handlers import clientplatform_creative_studio as studio


def _labels_and_callbacks(markup):
    return [
        (button.text, button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]


def test_creative_studio_menu_exposes_image_and_video_entry_points() -> None:
    rows = _labels_and_callbacks(studio._menu_rows("business-token", None))
    assert ("✨ Создать картинку", "cpc:new:business-token") in rows
    assert ("🎬 Создать AI-видео", "cpc:video:business-token") in rows


def test_creative_studio_menu_labels_motion_fallback_truthfully() -> None:
    rows = _labels_and_callbacks(
        studio._menu_rows(
            "business-token",
            None,
            video_ready=True,
            video_mode="motion",
        )
    )
    assert ("🎞 Оживить картинку", "cpc:video:business-token") in rows
    assert ("🎬 Создать AI-видео", "cpc:video:business-token") not in rows


def test_prepared_video_keeps_video_edit_path(monkeypatch) -> None:
    active = SimpleNamespace(
        status=CreativeGenerationReceiptStatus.PREPARED,
        delivery_claimed_at=None,
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "video")
    monkeypatch.setattr(
        studio,
        "_receipt_callback",
        lambda action, token, receipt: f"receipt:{action}:{token}",
    )

    rows = _labels_and_callbacks(studio._menu_rows("business-token", active))
    assert ("✏️ Изменить описание", "cpc:video:business-token") in rows


def test_result_menu_keeps_video_creation_visible() -> None:
    rows = _labels_and_callbacks(studio._result_rows("business-token"))
    assert ("✨ Создать ещё картинку", "cpc:new:business-token") in rows
    assert ("🎬 Создать видео", "cpc:video:business-token") in rows

def test_prepared_image_can_switch_to_video(monkeypatch) -> None:
    active = SimpleNamespace(
        status=CreativeGenerationReceiptStatus.PREPARED,
        delivery_claimed_at=None,
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "image")
    monkeypatch.setattr(
        studio,
        "_receipt_callback",
        lambda action, token, receipt: f"receipt:{action}:{token}",
    )

    rows = _labels_and_callbacks(studio._menu_rows("business-token", active))
    assert ("🎬 Вместо этого видео", "cpc:video:business-token") in rows


def test_prepared_video_can_switch_to_image(monkeypatch) -> None:
    active = SimpleNamespace(
        status=CreativeGenerationReceiptStatus.PREPARED,
        delivery_claimed_at=None,
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "video")
    monkeypatch.setattr(
        studio,
        "_receipt_callback",
        lambda action, token, receipt: f"receipt:{action}:{token}",
    )

    rows = _labels_and_callbacks(studio._menu_rows("business-token", active))
    assert ("✨ Вместо этого картинка", "cpc:new:business-token") in rows



def test_creative_menu_shows_unavailable_generation_truthfully() -> None:
    rows = _labels_and_callbacks(
        studio._menu_rows(
            "business-token",
            None,
            image_ready=False,
            video_ready=False,
        )
    )
    assert (
        "⚠️ Картинки недоступны",
        "cpc:status:business-token:image",
    ) in rows
    assert (
        "⚠️ Видео недоступно",
        "cpc:status:business-token:video",
    ) in rows
    assert ("✨ Создать картинку", "cpc:new:business-token") not in rows
    assert ("🎬 Создать AI-видео", "cpc:video:business-token") not in rows
    assert ("🎞 Оживить картинку", "cpc:video:business-token") not in rows

def test_new_visual_button_surfaces_actionable_active_generation(monkeypatch) -> None:
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="cpc:new:business-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
        message=target,
    )
    state = SimpleNamespace(set_state=AsyncMock(), set_data=AsyncMock())
    active = SimpleNamespace(
        id="receipt-id",
        status=CreativeGenerationReceiptStatus.PROCESSING,
        delivery_claimed_at=None,
    )
    monkeypatch.setattr(
        studio,
        "_actor_for_callback",
        AsyncMock(return_value=SimpleNamespace(business_id="business-id")),
    )
    monkeypatch.setattr(studio, "_active", AsyncMock(return_value=active))
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio, "_menu_rows", lambda token, receipt: [["continue", token, receipt]])

    asyncio.run(studio._ask_creative_prompt(callback, state, kind="image"))

    callback.answer.assert_awaited_once_with()
    state.set_state.assert_not_awaited()
    state.set_data.assert_not_awaited()
    assert "незавершённая генерация" in target.answer.await_args.args[0]
    assert target.answer.await_args.kwargs["reply_markup"] == [["continue", "business-token", active]]


def test_stale_video_callback_fails_closed_when_provider_is_unavailable(monkeypatch) -> None:
    callback = SimpleNamespace(
        data="cpc:video:business-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
    )
    state = SimpleNamespace(set_state=AsyncMock(), set_data=AsyncMock())
    monkeypatch.setattr(
        studio,
        "_actor_for_callback",
        AsyncMock(return_value=SimpleNamespace(business_id="business-id")),
    )
    monkeypatch.setattr(studio, "_active", AsyncMock(return_value=None))
    monkeypatch.setattr(
        studio,
        "visual_video_generation_mode",
        lambda **_kwargs: "unavailable",
    )

    asyncio.run(studio._ask_creative_prompt(callback, state, kind="video"))

    state.set_state.assert_not_awaited()
    state.set_data.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert "недоступно" in callback.answer.await_args.args[0].casefold()


def test_motion_mode_prompt_does_not_claim_native_video(monkeypatch) -> None:
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="cpc:video:business-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
        message=target,
    )
    state = SimpleNamespace(set_state=AsyncMock(), set_data=AsyncMock())
    monkeypatch.setattr(
        studio,
        "_actor_for_callback",
        AsyncMock(return_value=SimpleNamespace(business_id="business-id")),
    )
    monkeypatch.setattr(studio, "_active", AsyncMock(return_value=None))
    monkeypatch.setattr(
        studio,
        "visual_video_generation_mode",
        lambda **_kwargs: "motion",
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio.control, "_keyboard", lambda rows: rows)

    asyncio.run(studio._ask_creative_prompt(callback, state, kind="video"))

    state.set_state.assert_awaited_once()
    text = target.answer.await_args.args[0]
    assert "Это не генерация движущейся сцены" in text
    assert "AI-кадр" in text


def test_video_status_explains_motion_fallback(monkeypatch) -> None:
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="cpc:status:business-token:video",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
        message=target,
    )
    monkeypatch.setattr(studio, "_actor_for_callback", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        studio,
        "visual_video_generation_mode",
        lambda **_kwargs: "motion",
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio.control, "_keyboard", lambda rows: rows)

    asyncio.run(studio.creative_provider_status(callback))

    text = target.answer.await_args.args[0]
    assert "оживление AI-кадра" in text
    assert "не генерация движущейся сцены" in text
