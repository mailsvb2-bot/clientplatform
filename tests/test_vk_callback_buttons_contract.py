from __future__ import annotations

import asyncio
import json
from pathlib import Path

from keyboards.inline import kb_main
from runtime.messenger_ingress import (
    VK_PROCESSABLE_EVENT_TYPES,
    _entry_start_text,
    _vk_dedupe_key,
    _vk_event_context,
    _vk_score_route_text,
)
from runtime.messenger_payloads import extract_vk_message, vk_event_key
from runtime.messenger_transport_errors import MessengerTransportError
from runtime.messenger_vk_sender import VkBotSender, _callback_keyboard_json, _strip_raw_vk_payment_links
from runtime.messenger_vk_ui import vk_clear_keyboard_json, vk_score_scale_keyboard_json, with_vk_keyboard
from runtime.telegram_button_parity import vk_keyboard_from_telegram


def test_vk_parity_keyboard_uses_callback_buttons() -> None:
    keyboard = json.loads(vk_keyboard_from_telegram(kb_main(None)))
    assert keyboard["inline"] is True
    actions = [button["action"] for row in keyboard["buttons"] for button in row]
    assert actions
    assert all(action["type"] == "callback" for action in actions)


def test_vk_sender_normalizes_small_text_buttons_to_callback_buttons() -> None:
    raw = {
        "one_time": False,
        "inline": False,
        "buttons": [[{"action": {"type": "text", "label": "Settings", "payload": json.dumps({"command": "settings"})}}]],
    }
    keyboard = json.loads(_callback_keyboard_json(json.dumps(raw)))
    assert keyboard["inline"] is True
    assert keyboard["buttons"][0][0]["action"]["type"] == "callback"
    assert json.loads(keyboard["buttons"][0][0]["action"]["payload"])["command"] == "settings"


def test_vk_sender_preserves_full_score_keyboard_as_text_keyboard() -> None:
    keyboard = json.loads(_callback_keyboard_json(vk_score_scale_keyboard_json()))
    actions = [button["action"] for row in keyboard["buttons"] for button in row]
    labels = [action["label"] for action in actions]

    assert keyboard["inline"] is False
    assert keyboard["one_time"] is True
    assert len(actions) == 23
    assert labels[:21] == [f"{value:+d}" if value else "0" for value in range(-10, 11)]
    assert labels[-2:] == ["📈 Прогресс", "⬅️ Назад"]
    assert all(action["type"] == "text" for action in actions)


def test_vk_message_event_is_processable_and_extracts_payload_command() -> None:
    payload = {
        "type": "message_event",
        "object": {
            "event_id": "evt-1",
            "user_id": 123,
            "peer_id": 123,
            "payload": {"command": "settings"},
        },
    }
    assert "message_event" in VK_PROCESSABLE_EVENT_TYPES
    assert _vk_dedupe_key(payload) == "evt-1:123"
    assert _vk_event_context(payload) == ("evt-1", "123", "123")
    extracted = extract_vk_message(payload)
    assert extracted is not None
    assert extracted["external_user_id"] == "123"
    assert extracted["text"] == "settings"


def test_vk_message_event_score_one_two_are_preserved_as_score_not_demo() -> None:
    score_one = {
        "type": "message_event",
        "object": {
            "event_id": "evt-score-1",
            "user_id": 123,
            "peer_id": 123,
            "payload": {"command": "score:1"},
        },
    }
    score_two = {
        "type": "message_event",
        "object": {
            "event_id": "evt-score-2",
            "user_id": 123,
            "peer_id": 123,
            "payload": {"command": "score=2"},
        },
    }

    extracted_one = extract_vk_message(score_one)
    extracted_two = extract_vk_message(score_two)

    assert extracted_one is not None
    assert extracted_two is not None
    assert extracted_one["text"] == "1"
    assert extracted_two["text"] == "2"
    assert _entry_start_text(_vk_score_route_text(score_one) or extracted_one["text"]) == "+1"
    assert _entry_start_text(_vk_score_route_text(score_two) or extracted_two["text"]) == "+2"


def test_vk_message_event_dedupe_key_uses_event_id_not_only_user_id() -> None:
    first = {"type": "message_event", "object": {"event_id": "evt-1", "user_id": 123, "payload": {"command": "start"}}}
    second = {"type": "message_event", "object": {"event_id": "evt-2", "user_id": 123, "payload": {"command": "start"}}}
    assert _vk_dedupe_key(first) != _vk_dedupe_key(second)
    assert vk_event_key(first) != vk_event_key(second)


def test_vk_audio_upload_does_not_fall_back_to_doc_when_audio_message_scope_denied(monkeypatch, tmp_path) -> None:
    audio_path = tmp_path / "audio.ogg"
    audio_path.write_bytes(b"fake-audio")
    upload_types: list[str] = []

    async def fake_vk_method(self, method: str, params: dict):
        if method == "docs.getMessagesUploadServer":
            upload_type = str(params["type"])
            upload_types.append(upload_type)
            if upload_type == "audio_message":
                raise MessengerTransportError("audio_message scope denied")
            return {"response": {"upload_url": "https://upload.example"}}
        raise AssertionError(f"unexpected VK method: {method}")

    monkeypatch.setattr(VkBotSender, "_vk_method", fake_vk_method)
    monkeypatch.setattr("runtime.messenger_vk_sender.get_cached_media_token", lambda *args, **kwargs: None)

    import pytest
    with pytest.raises(MessengerTransportError, match="audio_message scope denied"):
        asyncio.run(VkBotSender(token="token")._ensure_doc_attachment("123", audio_path))

    assert upload_types == ["audio_message"]


def test_vk_payment_text_strips_raw_links_after_button_extraction() -> None:
    text = """💳 Тарифы Метротерапии

Стартовый пакет — 1 900 ₽
7 практик. Мягкий вход и проверка формата.
https://metrotherapy-bot.metrotherapy.ru/pay/yookassa?source=vk&package_id=practice_start_7&intent=very_long

Полный маршрут — 7 900 ₽
60 практик. Базовый месячный маршрут.
https://metrotherapy-bot.metrotherapy.ru/pay/yookassa?source=vk&package_id=practice_60&intent=very_long

После оплаты вернитесь сюда и нажмите «🎧 Получить аудио»."""

    cleaned = _strip_raw_vk_payment_links(text)

    assert "https://" not in cleaned
    assert "Стартовый пакет — 1 900 ₽" in cleaned
    assert "Полный маршрут — 7 900 ₽" in cleaned
    assert "После оплаты вернитесь сюда" in cleaned


def test_vk_image_upload_uses_photo_api_not_doc(monkeypatch, tmp_path) -> None:
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"fake-png")
    methods: list[tuple[str, dict]] = []
    stored: list[tuple] = []

    async def fake_vk_method(self, method: str, params: dict):
        methods.append((method, dict(params)))
        if method == "photos.getMessagesUploadServer":
            return {"response": {"upload_url": "https://upload.example/photo"}}
        if method == "photos.saveMessagesPhoto":
            assert params["server"] == "123"
            assert params["photo"] == "[{}]"
            assert params["hash"] == "hash"
            return {"response": [{"owner_id": 1, "id": 2, "access_key": "photo_key"}]}
        if method == "messages.send":
            assert params["attachment"] == "photo1_2_photo_key"
            return {"response": 99}
        raise AssertionError(f"unexpected VK method: {method}")

    def fake_multipart_upload(upload_url: str, *, field_name: str, path: Path):
        assert upload_url == "https://upload.example/photo"
        assert field_name == "photo"
        assert path == image_path
        return {"server": 123, "photo": "[{}]", "hash": "hash"}

    monkeypatch.setattr(VkBotSender, "_vk_method", fake_vk_method)
    monkeypatch.setattr("runtime.messenger_vk_sender.multipart_upload", fake_multipart_upload)
    monkeypatch.setattr("runtime.messenger_vk_sender.get_cached_media_token", lambda *args, **kwargs: None)
    monkeypatch.setattr("runtime.messenger_vk_sender.store_media_token", lambda *args, **kwargs: stored.append((args, kwargs)))

    result = asyncio.run(VkBotSender(token="token").send_image_file("123", image_path, caption="chart"))

    assert result == 99
    assert [method for method, _ in methods] == [
        "photos.getMessagesUploadServer",
        "photos.saveMessagesPhoto",
        "messages.send",
    ]
    assert stored
    assert stored[0][0][0] == "vk"
    assert stored[0][0][2] == "photo1_2_photo_key"


def test_vk_clear_keyboard_stays_regular_empty_keyboard() -> None:
    keyboard = json.loads(_callback_keyboard_json(vk_clear_keyboard_json()))

    assert keyboard["inline"] is False
    assert keyboard["one_time"] is True
    assert keyboard["buttons"] == []


def test_vk_ordinary_message_does_not_attach_main_settings_keyboard() -> None:
    kwargs = with_vk_keyboard(
        "vk",
        {"_text_for_keyboard": "✅ Оценку после прослушивания +3 сохранил."},
        user_id=123,
    )

    keyboard = json.loads(kwargs["keyboard_json"])
    dumped = json.dumps(keyboard, ensure_ascii=False)

    assert keyboard["inline"] is False
    assert keyboard["one_time"] is True
    assert keyboard["buttons"] == []
    assert "🧠 Настройки" not in dumped
