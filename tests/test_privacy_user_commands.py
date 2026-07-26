from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from handlers import info


class FakeMessage:
    def __init__(self, user_id: int, text: str = "", *, chat_type: str = "private") -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type=chat_type)
        self.text = text
        self.answers: list[str] = []
        self.documents: list[tuple[object, str | None]] = []
        self.document_paths_during_send: list[Path] = []

    async def answer(self, text: str, **_kwargs) -> None:
        self.answers.append(text)

    async def answer_document(self, document, *, caption: str | None = None, **_kwargs) -> None:
        path = Path(document.path)
        assert path.exists()
        self.document_paths_during_send.append(path)
        self.documents.append((document, caption))


class FailingDocumentMessage(FakeMessage):
    async def answer_document(self, document, *, caption: str | None = None, **_kwargs) -> None:
        path = Path(document.path)
        assert path.exists()
        self.document_paths_during_send.append(path)
        raise OSError("synthetic Telegram upload failure")


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
async def test_export_uses_authenticated_user_and_removes_temp_file(monkeypatch) -> None:
    seen: list[int] = []
    generated_paths: list[Path] = []

    def fake_export(user_id: int, output_path: str | Path):
        seen.append(user_id)
        path = Path(output_path)
        path.write_bytes(b"synthetic-gzip")
        generated_paths.append(path)
        return SimpleNamespace(path=path, total_rows=7, compressed_size_bytes=14)

    monkeypatch.setattr(info, "write_user_data_export_gzip", fake_export)
    message = FakeMessage(91001, "/mydata CONFIRM")

    await info.cmd_my_data(message)

    assert seen == [91001]
    assert len(message.documents) == 1
    document, caption = message.documents[0]
    assert str(document.filename) == "metrotherapy-user-data.json.gz"
    assert caption is not None and "Записей: 7" in caption
    assert "не зашифрован" in caption
    assert not message.answers
    assert message.document_paths_during_send == generated_paths
    assert all(not path.exists() for path in generated_paths)


@pytest.mark.asyncio
async def test_export_temp_file_is_removed_when_upload_fails(monkeypatch) -> None:
    generated_paths: list[Path] = []

    def fake_export(user_id: int, output_path: str | Path):
        path = Path(output_path)
        path.write_bytes(f"user={user_id}".encode())
        generated_paths.append(path)
        return SimpleNamespace(path=path, total_rows=1, compressed_size_bytes=10)

    monkeypatch.setattr(info, "write_user_data_export_gzip", fake_export)
    message = FailingDocumentMessage(91004, "/mydata CONFIRM")

    await info.cmd_my_data(message)

    assert generated_paths
    assert all(not path.exists() for path in generated_paths)
    assert message.answers
    assert "Не удалось подготовить экспорт" in message.answers[-1]


@pytest.mark.asyncio
async def test_export_requires_confirmation_and_private_chat(monkeypatch) -> None:
    called = False

    def fake_export(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not export without confirmation or from a group")

    monkeypatch.setattr(info, "write_user_data_export_gzip", fake_export)

    unconfirmed = FakeMessage(91005, "/mydata")
    await info.cmd_my_data(unconfirmed)
    assert called is False
    assert "/mydata CONFIRM" in unconfirmed.answers[-1]
    assert "не зашифрован" in unconfirmed.answers[-1]

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
