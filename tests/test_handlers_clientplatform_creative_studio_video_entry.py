from __future__ import annotations

from types import SimpleNamespace

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
    assert ("🎬 Создать видео", "cpc:video:business-token") in rows


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
