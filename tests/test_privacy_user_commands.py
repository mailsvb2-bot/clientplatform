from __future__ import annotations

from types import SimpleNamespace

import pytest

from handlers import info


class FakeMessage:
    def __init__(self, user_id: int, text: str = "", *, chat_type: str = "private") -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type=chat_type)
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs) -> None:
        self.answers.append(text)


def test_delete_confirmation_is_exact() -> None:
    assert info._delete_confirmed("/deletemydata CONFIRM") is True
    assert info._delete_confirmed("/deletemydata confirm") is True
    assert info._delete_confirmed("/deletemydata") is False
    assert info._delete_confirmed("/deletemydata YES") is False
    assert info._delete_confirmed("/deletemydata CONFIRM extra") is False


def test_export_confirmation_is_exact() -> None:
    assert info._export_confirmed("/mydata CONFIRM") is True
    assert info._export_confirmed("/mydata@metrotherapybot confirm") is True
    assert info._export_confirmed("/mydata") is False
    assert info._export_confirmed("mydata CONFIRM") is False
    assert info._export_confirmed("/mydata YES") is False
    assert info._export_confirmed("/mydata CONFIRM extra") is False


@pytest.mark.asyncio
async def test_export_issues_authenticated_one_time_link(monkeypatch) -> None:
    seen: list[tuple[int, str]] = []

    def issue(user_id: int, *, platform: str) -> str:
        seen.append((user_id, platform))
        return "https://example.test/privacy/export/random-token"

    monkeypatch.setattr(info, "issue_privacy_export_url", issue)
    monkeypatch.setattr(info, "privacy_export_ttl_minutes", lambda: 10)
    message = FakeMessage(91001, "/mydata CONFIRM")

    await info.cmd_my_data(message)

    assert seen == [(91001, "telegram")]
    assert len(message.answers) == 1
    assert "https://example.test/privacy/export/random-token" in message.answers[0]
    assert "одноразовая" in message.answers[0].casefold()
    assert "предпросмотр" in message.answers[0].casefold()


@pytest.mark.asyncio
async def test_export_fails_closed_without_secure_public_link(monkeypatch) -> None:
    monkeypatch.setattr(info, "issue_privacy_export_url", lambda *_args, **_kwargs: "")
    message = FakeMessage(91004, "/mydata CONFIRM")

    await info.cmd_my_data(message)

    assert message.answers
    assert "Не удалось подготовить экспорт" in message.answers[-1]


@pytest.mark.asyncio
async def test_export_requires_confirmation_and_private_chat(monkeypatch) -> None:
    called = False

    def issue(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not issue without confirmation or from a group")

    monkeypatch.setattr(info, "issue_privacy_export_url", issue)

    unconfirmed = FakeMessage(91005, "/mydata")
    await info.cmd_my_data(unconfirmed)
    assert called is False
    assert "/mydata CONFIRM" in unconfirmed.answers[-1]
    assert "одноразовую HTTPS-ссылку" in unconfirmed.answers[-1]

    group = FakeMessage(91005, "/mydata CONFIRM", chat_type="group")
    await info.cmd_my_data(group)
    assert called is False
    assert "только в личном чате" in group.answers[-1]


@pytest.mark.asyncio
async def test_delete_without_confirmation_does_not_mutate(monkeypatch) -> None:
    called = False

    def fake_erase(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not erase without confirmation")

    monkeypatch.setattr(info, "erase_user_behavioral_data", fake_erase)
    message = FakeMessage(91002, "/deletemydata")

    await info.cmd_delete_my_data(message)

    assert called is False
    assert message.answers
    assert "/deletemydata CONFIRM" in message.answers[0]
    assert "Технический идентификатор канала" in message.answers[0]
    assert "обезличит профиль" not in message.answers[0]


@pytest.mark.asyncio
async def test_confirmed_delete_uses_authenticated_message_user(monkeypatch) -> None:
    seen: list[tuple[int, str]] = []

    def fake_erase(user_id: int, *, reason: str):
        seen.append((user_id, reason))
        return SimpleNamespace(deleted_tables={"events": 3, "jobs": 2})

    monkeypatch.setattr(info, "erase_user_behavioral_data", fake_erase)
    message = FakeMessage(91003, "/deletemydata CONFIRM")

    await info.cmd_delete_my_data(message)

    assert seen == [(91003, "telegram_user_request")]
    assert message.answers
    assert "Удалено записей: 5" in message.answers[-1]
    assert "Технический идентификатор канала" in message.answers[-1]
    assert "профиль обезличен" not in message.answers[-1]
