from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import settings
from runtime import messenger_max_ui as max_ui
from runtime.messenger_payloads import normalise_messenger_text
from services.accounts.identity import resolve_account_for_identity
from services.messenger.audio_progress import AudioProgressItem, get_progress_snapshot
from services.messenger.reply_dispatcher import send_reply_bundle
from services.messenger.text_ui import MessengerReply
from services.messenger.text_ui_router import handle_incoming_text
from services.practice_tokens import get_wallet, grant_tokens


@dataclass(frozen=True)
class _AnchoredAudio:
    anchor: int
    clean_title: str
    path: Path


class _FakeMaxSender:
    def __init__(self, *, fail_audio: bool = False) -> None:
        self.fail_audio = fail_audio
        self.audio_calls: list[tuple[str, Path, str | None, dict[str, Any]]] = []
        self.text_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def send_audio_file(
        self,
        external_user_id: str,
        file_path: Path,
        *,
        caption: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.audio_calls.append((str(external_user_id), Path(file_path), caption, dict(kwargs)))
        if self.fail_audio:
            raise RuntimeError("MAX rejected this audio attachment")
        return {"ok": True}

    async def send_text(self, external_user_id: str, text: str, **kwargs: Any) -> dict[str, Any]:
        stored = dict(kwargs)
        attachments = list(stored.get("attachments") or max_ui.native_keyboard_attachments(str(text or "")))
        if attachments:
            stored["attachments"] = attachments
        self.text_calls.append(
            (
                str(external_user_id),
                max_ui.prepare_text(str(text or ""), has_native_keyboard=bool(attachments)),
                stored,
            )
        )
        return {"ok": True}


def _attachments(sender: _FakeMaxSender) -> list[dict[str, Any]]:
    for _, _, kwargs in reversed(sender.text_calls):
        attachments = list(kwargs.get("attachments") or [])
        if attachments:
            return attachments
    raise AssertionError("no MAX attachment captured")


def _button_commands(attachment: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    for row in attachment.get("payload", {}).get("buttons", []):
        for button in row:
            payload = button.get("payload") or {}
            command = payload.get("command")
            if command is not None:
                commands.append(str(command))
    return commands


def _button_texts(attachment: dict[str, Any]) -> list[str]:
    return [
        str(button.get("text") or "")
        for row in attachment.get("payload", {}).get("buttons", [])
        for button in row
    ]


def _link_buttons(attachment: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        button
        for row in attachment.get("payload", {}).get("buttons", [])
        for button in row
        if button.get("type") == "link"
    ]


async def _dispatch_max(user_id: int, text: str) -> list[MessengerReply]:
    normalized = normalise_messenger_text(text)
    canonical_user_id, replies = handle_incoming_text(
        user_id,
        platform="max",
        external_user_id=str(user_id),
        text=normalized,
    )
    await send_reply_bundle("max", str(user_id), canonical_user_id, replies)
    return replies


def test_max_full_user_journey_score_audio_done_repeat_pay_gift(monkeypatch, tmp_path):
    user_id = 928000000 + (os.getpid() % 100000)

    source_path = tmp_path / "01_morning.mp3"
    opus_path = tmp_path / "01_morning.opus"
    source_path.write_bytes(b"fake-max-source-audio")
    opus_path.write_bytes(b"fake-max-opus-audio")
    item = AudioProgressItem(ordinal=1, anchor=1, title="Morning Route", path=source_path)
    anchored = _AnchoredAudio(anchor=1, clean_title="Morning Route", path=source_path)

    sender = _FakeMaxSender()
    monkeypatch.setenv("TOKEN_ENFORCEMENT_MODE", "hard")
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "1")
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("PAYMENT_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setattr(settings, "MESSENGER_PUBLIC_BASE_URL", "https://example.test", raising=False)
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_PUBLIC_BASE_URL", "https://example.test", raising=False)
    monkeypatch.setattr("services.messenger.audio_progress.list_full_series", lambda: [item])
    monkeypatch.setattr("services.mood_text_flow.get_by_anchor", lambda anchor: anchored if int(anchor) == 1 else None)
    monkeypatch.setattr("services.mood_text_flow.ensure_max_opus_file", lambda path: opus_path)
    monkeypatch.setattr("services.messenger.audio_delivery.ensure_max_opus_file", lambda path: opus_path)
    monkeypatch.setattr("services.messenger.reply_dispatcher.MaxBotSender", lambda: sender)
    monkeypatch.setattr("services.messenger.reply_dispatcher.build_vk_mood_progress_chart_path", lambda uid: None)

    asyncio.run(_dispatch_max(user_id, "start"))
    canonical_user_id = resolve_account_for_identity(
        "max", str(user_id), allow_create=False
    )
    assert canonical_user_id is not None
    grant_tokens(
        canonical_user_id,
        package_id="practice_start_7",
        amount=3,
        provider="test",
        provider_payment_id=f"max-e2e-{user_id}",
        idempotency_key=f"grant:test:max-e2e:{user_id}",
    )
    assert "Главное меню" in sender.text_calls[-1][1]
    assert _button_commands(_attachments(sender)[0])

    asyncio.run(_dispatch_max(user_id, "continue"))
    assert "Следующая практика" in sender.text_calls[-1][1]
    score_attachment = _attachments(sender)[0]
    assert _button_commands(score_attachment)[:21] == [f"score:{value}" for value in range(-10, 11)]
    assert _button_texts(score_attachment)[:21] == [str(value) for value in range(-10, 11)]

    asyncio.run(_dispatch_max(user_id, "-9"))
    assert sender.audio_calls
    external_id, file_path, caption, _ = sender.audio_calls[-1]
    assert external_id == str(user_id)
    assert file_path == opus_path
    assert "Ваш аудиотранс" in (caption or "")
    post_audio_attachment = _attachments(sender)[0]
    assert "done" in _button_commands(post_audio_attachment)
    snapshot = get_progress_snapshot(canonical_user_id)
    assert snapshot.pending_item is not None
    assert snapshot.pending_item.anchor == 1
    assert get_wallet(canonical_user_id).used_tokens == 1

    asyncio.run(_dispatch_max(user_id, "done"))
    post_score_attachment = _attachments(sender)[0]
    assert "Теперь оцените состояние ПОСЛЕ" in sender.text_calls[-1][1]
    assert _button_commands(post_score_attachment)[:21] == [f"score:{value}" for value in range(-10, 11)]

    asyncio.run(_dispatch_max(user_id, "3"))
    joined_texts = "\n".join(text for _, text, _ in sender.text_calls[-4:])
    assert "Оценку после прослушивания +3 сохранил" in joined_texts

    audio_count_before_repeat = len(sender.audio_calls)
    wallet_before_repeat = get_wallet(canonical_user_id)
    asyncio.run(_dispatch_max(user_id, "repeat"))
    assert len(sender.audio_calls) == audio_count_before_repeat + 1
    assert get_wallet(canonical_user_id) == wallet_before_repeat
    repeat_text = sender.text_calls[-1][1]
    assert "Повторно отправил аудио" in repeat_text
    assert "done" in _button_commands(_attachments(sender)[0])

    asyncio.run(_dispatch_max(user_id, "pay"))
    pay_links = _link_buttons(_attachments(sender)[0])
    assert pay_links
    assert all(str(button.get("url") or "").startswith("https://example.test/") for button in pay_links)

    asyncio.run(_dispatch_max(user_id, "gift"))
    assert "кому" in sender.text_calls[-1][1].casefold()
    assert not list(sender.text_calls[-1][2].get("attachments") or [])

    asyncio.run(_dispatch_max(user_id, "Анна +79990001122"))
    gift_links = _link_buttons(_attachments(sender)[0])
    assert gift_links
    assert all(str(button.get("url") or "").startswith("https://example.test/") for button in gift_links)
    assert all("gift=1" in str(button.get("url") or "") for button in gift_links)
