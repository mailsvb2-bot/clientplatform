from __future__ import annotations

from types import SimpleNamespace

import pytest

from handlers import clientplatform_privacy as privacy


class FakeMessage:
    def __init__(self, user_id: int, text: str = '', *, chat_type: str = 'private') -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type=chat_type)
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs) -> None:
        self.answers.append(text)


def test_privacy_confirmations_are_exact() -> None:
    assert privacy._confirmed('/mydata CONFIRM', 'mydata') is True
    assert privacy._confirmed('/mydata@clientplatformbot confirm', 'mydata') is True
    assert privacy._confirmed('/mydata', 'mydata') is False
    assert privacy._confirmed('/mydata YES', 'mydata') is False
    assert privacy._confirmed('/mydata CONFIRM extra', 'mydata') is False
    assert privacy._confirmed('/deletemydata CONFIRM', 'deletemydata') is True
    assert privacy._confirmed('/deletemydata YES', 'deletemydata') is False


@pytest.mark.asyncio
async def test_export_issues_authenticated_one_time_link(monkeypatch) -> None:
    seen: list[tuple[int, str]] = []

    def issue(user_id: int, *, platform: str) -> str:
        seen.append((user_id, platform))
        return 'https://example.test/privacy/export/random-token'

    monkeypatch.setattr(privacy.control, '_user_id', lambda message: int(message.from_user.id))
    monkeypatch.setattr(privacy, 'issue_privacy_export_url', issue)
    monkeypatch.setattr(privacy, 'privacy_export_ttl_minutes', lambda: 10)
    message = FakeMessage(91001, '/mydata CONFIRM')

    await privacy.clientplatform_export_data(message)

    assert seen == [(91001, 'telegram')]
    assert len(message.answers) == 1
    assert 'https://example.test/privacy/export/random-token' in message.answers[0]
    assert 'одноразовая' in message.answers[0].casefold()
    assert 'предпросмотр' in message.answers[0].casefold()


@pytest.mark.asyncio
async def test_export_requires_confirmation_and_private_chat(monkeypatch) -> None:
    called = False

    def issue(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError('must not issue without confirmation or from a group')

    monkeypatch.setattr(privacy, 'issue_privacy_export_url', issue)
    unconfirmed = FakeMessage(91005, '/mydata')
    await privacy.clientplatform_export_data(unconfirmed)
    assert called is False
    assert '/mydata CONFIRM' in unconfirmed.answers[-1]

    group = FakeMessage(91005, '/mydata CONFIRM', chat_type='group')
    await privacy.clientplatform_export_data(group)
    assert called is False
    assert 'только в личном чате' in group.answers[-1]


@pytest.mark.asyncio
async def test_delete_requires_confirmation_and_uses_authenticated_user(monkeypatch) -> None:
    seen: list[tuple[int, str]] = []

    def erase(user_id: int, *, reason: str):
        seen.append((user_id, reason))
        return SimpleNamespace(deleted_tables={'events': 3, 'jobs': 2})

    monkeypatch.setattr(privacy.control, '_user_id', lambda message: int(message.from_user.id))
    monkeypatch.setattr(privacy, 'erase_user_behavioral_data', erase)

    unconfirmed = FakeMessage(91003, '/deletemydata')
    await privacy.clientplatform_delete_data(unconfirmed)
    assert seen == []
    assert '/deletemydata CONFIRM' in unconfirmed.answers[-1]

    confirmed = FakeMessage(91003, '/deletemydata CONFIRM')
    await privacy.clientplatform_delete_data(confirmed)
    assert seen == [(91003, 'telegram_user_request')]
    assert 'Удалено записей: 5' in confirmed.answers[-1]
