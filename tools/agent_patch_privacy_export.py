from __future__ import annotations

import subprocess
from pathlib import Path


def replace_exact(path: str, old: str, new: str, *, count: int = 1) -> None:
    target = Path(path)
    source = target.read_text(encoding="utf-8")
    actual = source.count(old)
    if actual != count:
        raise SystemExit(f"{path}: expected {count} occurrences, found {actual}: {old[:80]!r}")
    target.write_text(source.replace(old, new, count), encoding="utf-8")


replace_exact(
    "handlers/info.py",
    "from aiogram import Router\nfrom aiogram.filters import Command\n",
    "from aiogram import Router\nfrom aiogram.enums import ChatType\nfrom aiogram.filters import Command\n",
)
replace_exact(
    "handlers/info.py",
    '''def _delete_confirmed(text: str | None) -> bool:\n    parts = str(text or "").strip().split(maxsplit=1)\n    return len(parts) == 2 and parts[1].strip().upper() == "CONFIRM"\n\n\ndef _new_export_path(user_id: int) -> Path:\n    handle = tempfile.NamedTemporaryFile(\n        prefix=f"metrotherapy-user-data-{int(user_id)}-",\n        suffix=".json.gz",\n        delete=False,\n    )\n''',
    '''def _delete_confirmed(text: str | None) -> bool:\n    parts = str(text or "").strip().split(maxsplit=1)\n    return len(parts) == 2 and parts[1].strip().upper() == "CONFIRM"\n\n\ndef _export_confirmed(text: str | None) -> bool:\n    parts = str(text or "").strip().split(maxsplit=1)\n    if len(parts) != 2 or parts[1].strip().upper() != "CONFIRM":\n        return False\n    command = parts[0].strip().casefold().split("@", maxsplit=1)[0]\n    return command == "/mydata"\n\n\ndef _is_private_chat(message: Message) -> bool:\n    chat = getattr(message, "chat", None)\n    raw_type = getattr(chat, "type", None)\n    value = getattr(raw_type, "value", raw_type)\n    return str(value or "").strip().casefold() == ChatType.PRIVATE.value\n\n\ndef _new_export_path() -> Path:\n    handle = tempfile.NamedTemporaryFile(\n        prefix="metrotherapy-user-data-",\n        suffix=".json.gz",\n        delete=False,\n    )\n''',
)
replace_exact(
    "handlers/info.py",
    '        "Получить копию своих данных: /mydata\\n"\n',
    '        "Получить копию своих данных: /mydata — затем /mydata CONFIRM\\n"\n',
)
replace_exact(
    "handlers/info.py",
    '''    if user_id is None:\n        return\n\n    export_path: Path | None = None\n    try:\n        export_path = _new_export_path(user_id)\n''',
    '''    if user_id is None:\n        return\n    if not _is_private_chat(message):\n        await message.answer(\n            "🔐 Экспорт данных доступен только в личном чате с ботом. "\n            "Откройте личный диалог и отправьте /mydata заново."\n        )\n        return\n    if not _export_confirmed(message.text):\n        await message.answer(\n            "⚠️ Экспорт может содержать историю использования, оценки состояния и платёжные записи. "\n            "Архив сжат, но не зашифрован, и после отправки останется в истории этого чата.\\n\\n"\n            "Для подтверждения отправьте точно:\\n"\n            "/mydata CONFIRM"\n        )\n        return\n\n    export_path: Path | None = None\n    try:\n        export_path = _new_export_path()\n''',
)
replace_exact(
    "handlers/info.py",
    '            filename=f"metrotherapy-user-data-{user_id}.json.gz",\n',
    '            filename="metrotherapy-user-data.json.gz",\n',
)
replace_exact(
    "handlers/info.py",
    '''                "🔐 Это сжатый JSON-экспорт данных, связанных с Вашим аккаунтом. "\n                f"Записей: {result.total_rows}. "\n                "Файл может содержать историю использования и платёжные записи — храните его безопасно."\n''',
    '''                "🔐 Это сжатый JSON-экспорт данных, связанных с Вашим аккаунтом. "\n                f"Записей: {result.total_rows}. "\n                "Архив не зашифрован. Сохраните его только в защищённом месте и удалите это сообщение, "\n                "когда файл больше не нужен в истории чата."\n''',
)

replace_exact(
    "services/privacy_controls.py",
    '''    path = Path(output_path)\n    path.parent.mkdir(parents=True, exist_ok=True)\n    exported_at = _utc_now_iso()\n''',
    '''    path = Path(output_path)\n    path.parent.mkdir(parents=True, exist_ok=True)\n    path.touch(mode=0o600, exist_ok=True)\n    path.chmod(0o600)\n    exported_at = _utc_now_iso()\n''',
)

replace_exact(
    "services/messenger/text_ui_router.py",
    '''    if normalized in {"mydata", "/mydata", "мои данные", "экспорт данных", "выгрузить данные"}:\n        return "export", False\n\n    delete_aliases = (\n''',
    '''    export_aliases = (\n        "mydata",\n        "/mydata",\n        "мои данные",\n        "экспорт данных",\n        "выгрузить данные",\n    )\n    for alias in export_aliases:\n        if normalized == alias:\n            return "export", False\n        if normalized == f"{alias} confirm":\n            return "export", True\n\n    delete_aliases = (\n''',
)
replace_exact(
    "services/messenger/text_ui_router.py",
    '''            "Получить сжатую копию данных: mydata или /mydata\\n"\n            "Удалить поведенческую историю: deletemydata\\n\\n"\n            "Удаление требует отдельного подтверждения словом CONFIRM. "\n''',
    '''            "Получить сжатую копию данных: mydata — затем mydata CONFIRM\\n"\n            "Удалить поведенческую историю: deletemydata\\n\\n"\n            "Экспорт и удаление требуют отдельного подтверждения словом CONFIRM. "\n''',
)
replace_exact(
    "services/messenger/text_ui_router.py",
    '''\n\ndef _privacy_delete_reply(user_id: int, *, platform: str, confirmed: bool) -> MessengerReply:\n''',
    '''\n\ndef _privacy_export_reply(*, confirmed: bool) -> MessengerReply:\n    if confirmed:\n        return MessengerReply(kind="privacy_export")\n    return MessengerReply(\n        text=(\n            "⚠️ Экспорт может содержать историю использования, оценки состояния и платёжные записи. "\n            "Архив сжат, но не зашифрован, и после отправки останется в истории этого чата.\\n\\n"\n            "Для подтверждения отправьте точно:\\n"\n            "mydata CONFIRM"\n        )\n    )\n\n\ndef _privacy_delete_reply(user_id: int, *, platform: str, confirmed: bool) -> MessengerReply:\n''',
)
replace_exact(
    "services/messenger/text_ui_router.py",
    '''        if action_name == "export":\n            return canonical_user_id, [MessengerReply(kind="privacy_export")]\n''',
    '''        if action_name == "export":\n            return canonical_user_id, [_privacy_export_reply(confirmed=confirmed)]\n''',
)

replace_exact(
    "services/messenger/reply_dispatcher.py",
    '    return root, root / f"metrotherapy-user-data-{int(user_id)}.json.gz"\n',
    '    return root, root / "metrotherapy-user-data.json.gz"\n',
)
replace_exact(
    "services/messenger/reply_dispatcher.py",
    '''            "🔐 Сжатый JSON-экспорт данных, связанных с Вашим аккаунтом. "\n            f"Записей: {result.total_rows}. "\n            "Файл может содержать историю использования и платежные записи — храните его безопасно."\n''',
    '''            "🔐 Сжатый JSON-экспорт данных, связанных с Вашим аккаунтом. "\n            f"Записей: {result.total_rows}. "\n            "Архив не зашифрован. Сохраните его только в защищённом месте и удалите сообщение, "\n            "когда файл больше не нужен в истории чата."\n''',
)

replace_exact(
    "tests/test_privacy_user_commands.py",
    '''class FakeMessage:\n    def __init__(self, user_id: int, text: str = "") -> None:\n        self.from_user = SimpleNamespace(id=user_id)\n        self.text = text\n''',
    '''class FakeMessage:\n    def __init__(self, user_id: int, text: str = "", *, chat_type: str = "private") -> None:\n        self.from_user = SimpleNamespace(id=user_id)\n        self.chat = SimpleNamespace(type=chat_type)\n        self.text = text\n''',
)
replace_exact(
    "tests/test_privacy_user_commands.py",
    '''def test_delete_confirmation_is_exact() -> None:\n    assert info._delete_confirmed("/deletemydata CONFIRM") is True\n    assert info._delete_confirmed("/deletemydata confirm") is True\n    assert info._delete_confirmed("/deletemydata") is False\n    assert info._delete_confirmed("/deletemydata YES") is False\n    assert info._delete_confirmed("/deletemydata CONFIRM extra") is False\n\n\n''',
    '''def test_delete_confirmation_is_exact() -> None:\n    assert info._delete_confirmed("/deletemydata CONFIRM") is True\n    assert info._delete_confirmed("/deletemydata confirm") is True\n    assert info._delete_confirmed("/deletemydata") is False\n    assert info._delete_confirmed("/deletemydata YES") is False\n    assert info._delete_confirmed("/deletemydata CONFIRM extra") is False\n\n\ndef test_export_confirmation_is_exact() -> None:\n    assert info._export_confirmed("/mydata CONFIRM") is True\n    assert info._export_confirmed("/mydata@metrotherapybot confirm") is True\n    assert info._export_confirmed("/mydata") is False\n    assert info._export_confirmed("mydata CONFIRM") is False\n    assert info._export_confirmed("/mydata YES") is False\n    assert info._export_confirmed("/mydata CONFIRM extra") is False\n\n\n''',
)
replace_exact(
    "tests/test_privacy_user_commands.py",
    '    message = FakeMessage(91001, "/mydata")\n',
    '    message = FakeMessage(91001, "/mydata CONFIRM")\n',
)
replace_exact(
    "tests/test_privacy_user_commands.py",
    '''    assert str(document.filename).endswith(".json.gz")\n    assert caption is not None and "Записей: 7" in caption\n''',
    '''    assert str(document.filename) == "metrotherapy-user-data.json.gz"\n    assert caption is not None and "Записей: 7" in caption\n    assert "не зашифрован" in caption\n''',
)
replace_exact(
    "tests/test_privacy_user_commands.py",
    '    message = FailingDocumentMessage(91004, "/mydata")\n',
    '    message = FailingDocumentMessage(91004, "/mydata CONFIRM")\n',
)
replace_exact(
    "tests/test_privacy_user_commands.py",
    '''\n\n@pytest.mark.asyncio\nasync def test_delete_without_confirmation_does_not_mutate(monkeypatch) -> None:\n''',
    '''\n\n@pytest.mark.asyncio\nasync def test_export_requires_confirmation_and_private_chat(monkeypatch) -> None:\n    called = False\n\n    def fake_export(*_args, **_kwargs):\n        nonlocal called\n        called = True\n        raise AssertionError("must not export without confirmation or from a group")\n\n    monkeypatch.setattr(info, "write_user_data_export_gzip", fake_export)\n\n    unconfirmed = FakeMessage(91005, "/mydata")\n    await info.cmd_my_data(unconfirmed)\n    assert called is False\n    assert "/mydata CONFIRM" in unconfirmed.answers[-1]\n    assert "не зашифрован" in unconfirmed.answers[-1]\n\n    group = FakeMessage(91005, "/mydata CONFIRM", chat_type="group")\n    await info.cmd_my_data(group)\n    assert called is False\n    assert "только в личном чате" in group.answers[-1]\n\n\n@pytest.mark.asyncio\nasync def test_delete_without_confirmation_does_not_mutate(monkeypatch) -> None:\n''',
)

replace_exact(
    "tests/test_privacy_payment_runtime_followup.py",
    '''    _, replies = text_ui_router.handle_incoming_text(\n        77,\n        platform="max",\n        external_user_id="max-77",\n        text="mydata",\n    )\n    assert replies == [text_ui_router.MessengerReply(kind="privacy_export")]\n\n    erased: list[tuple[int, str]] = []\n''',
    '''    _, warning = text_ui_router.handle_incoming_text(\n        77,\n        platform="max",\n        external_user_id="max-77",\n        text="mydata",\n    )\n    assert "mydata CONFIRM" in warning[0].text\n    assert "не зашифрован" in warning[0].text\n\n    _, replies = text_ui_router.handle_incoming_text(\n        77,\n        platform="max",\n        external_user_id="max-77",\n        text="mydata CONFIRM",\n    )\n    assert replies == [text_ui_router.MessengerReply(kind="privacy_export")]\n\n    erased: list[tuple[int, str]] = []\n''',
)
replace_exact(
    "tests/test_privacy_payment_runtime_followup.py",
    '''    assert observed["external_user_id"] == "vk-77"\n    assert observed["bytes"] == b"privacy-export"\n    assert "Записей: 4" in observed["caption"]\n''',
    '''    assert observed["external_user_id"] == "vk-77"\n    assert observed["bytes"] == b"privacy-export"\n    assert observed["generated_path"].name == "metrotherapy-user-data.json.gz"\n    assert "Записей: 4" in observed["caption"]\n    assert "не зашифрован" in observed["caption"]\n''',
)

replace_exact(
    "tests/test_privacy_streaming_export.py",
    "import json\nfrom types import SimpleNamespace\n",
    "import json\nimport stat\nfrom types import SimpleNamespace\n",
)
replace_exact(
    "tests/test_privacy_streaming_export.py",
    '''    assert result.compressed_size_bytes > 0\n    assert result.table_rows["users"] == 1\n''',
    '''    assert result.compressed_size_bytes > 0\n    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600\n    assert result.table_rows["users"] == 1\n''',
)

subprocess.run(
    [
        "python",
        "-m",
        "py_compile",
        "handlers/info.py",
        "services/privacy_controls.py",
        "services/messenger/text_ui_router.py",
        "services/messenger/reply_dispatcher.py",
        "tests/test_privacy_user_commands.py",
        "tests/test_privacy_payment_runtime_followup.py",
        "tests/test_privacy_streaming_export.py",
    ],
    check=True,
)
subprocess.run(
    [
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/test_privacy_user_commands.py",
        "tests/test_privacy_payment_runtime_followup.py",
        "tests/test_privacy_streaming_export.py",
    ],
    check=True,
)
