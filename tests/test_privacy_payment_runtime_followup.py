from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime.messenger_max_sender import MaxBotSender
from services.messenger import reply_dispatcher, text_ui_router


@pytest.mark.asyncio
async def test_max_sender_uploads_privacy_export_as_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sender = MaxBotSender()
    calls: list[dict[str, Any]] = []

    async def send_media(*args: Any, **kwargs: Any) -> str:
        calls.append({"args": args, "kwargs": kwargs})
        return "sent"

    monkeypatch.setattr(sender, "_send_media_file", send_media)
    export_path = tmp_path / "export.json.gz"
    export_path.write_bytes(b"data")

    result = await sender.send_document_file(
        "max-77",
        export_path,
        caption="private",
        notify=False,
    )

    assert result == "sent"
    assert calls[0]["args"] == ("max-77", export_path)
    assert calls[0]["kwargs"] == {
        "media_type": "file",
        "caption": "private",
        "notify": False,
    }
