from __future__ import annotations

from typing import Any

import pytest

from clientplatform.runtime.dispatch_runtime import DispatchRuntimeConfig, build_dispatch_runtime
from clientplatform.transport.telegram_http import AiohttpTelegramBotClient


@pytest.mark.asyncio
async def test_private_gateway_media_uses_multipart_with_selected_bot_token() -> None:
    json_calls: list[tuple[str, dict[str, str]]] = []
    multipart_calls: list[tuple[str, str, str, str, float, int]] = []

    async def post_json(
        url: str,
        payload: dict[str, str],
        _timeout: float,
    ) -> tuple[int, Any]:
        json_calls.append((url, payload))
        return 200, {"ok": True, "result": {"message_id": 10}}

    async def post_multipart(
        url: str,
        field_name: str,
        chat_id: str,
        media_url: str,
        timeout: float,
        max_bytes: int,
    ) -> tuple[int, Any]:
        multipart_calls.append(
            (url, field_name, chat_id, media_url, timeout, max_bytes)
        )
        return 200, {"ok": True, "result": {"message_id": 11}}

    client = AiohttpTelegramBotClient(
        timeout_seconds=25,
        post_json=post_json,
        post_multipart=post_multipart,
        multipart_media_base_url="https://client.example/clientplatform",
        multipart_max_bytes=20_000_000,
    )
    reference = (
        "https://client.example/clientplatform/media/clientplatform-production/"
        "program-media/object.pdf?expires=1&sig=x"
    )

    result = await client.send_document(
        token="business-bot-token",
        chat_id="42",
        document=reference,
    )

    assert result == "11"
    assert json_calls == []
    assert len(multipart_calls) == 1
    url, field, chat_id, selected_reference, timeout, max_bytes = multipart_calls[0]
    assert url.endswith("/botbusiness-bot-token/sendDocument")
    assert field == "document"
    assert chat_id == "42"
    assert selected_reference == reference
    assert timeout == 25
    assert max_bytes == 20_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    [
        "control-bot-local-file-id",
        "https://public.example/material.pdf",
        "https://client.example/other/media/object.pdf",
        "https://foreign.example/clientplatform/media/object.pdf",
    ],
)
async def test_non_gateway_references_never_trigger_server_side_fetch(reference: str) -> None:
    json_calls: list[dict[str, str]] = []
    multipart_calls: list[str] = []

    async def post_json(
        _url: str,
        payload: dict[str, str],
        _timeout: float,
    ) -> tuple[int, Any]:
        json_calls.append(payload)
        return 200, {"ok": True, "result": {"message_id": 12}}

    async def post_multipart(*args: Any) -> tuple[int, Any]:
        multipart_calls.append(str(args))
        return 200, {"ok": True, "result": {"message_id": 13}}

    client = AiohttpTelegramBotClient(
        post_json=post_json,
        post_multipart=post_multipart,
        multipart_media_base_url="https://client.example/clientplatform",
    )
    assert await client.send_document(
        token="business-bot-token",
        chat_id="42",
        document=reference,
    ) == "12"
    assert json_calls == [{"chat_id": "42", "document": reference}]
    assert multipart_calls == []


def test_dispatch_runtime_binds_gateway_and_shared_size_limit() -> None:
    runtime = build_dispatch_runtime(
        DispatchRuntimeConfig(
            enabled=True,
            interval_seconds=5,
            tick_timeout_seconds=30,
            batch_size=10,
            max_attempts=5,
            lock_ttl_seconds=300,
            http_timeout_seconds=20,
            media_gateway_base_url="https://client.example/clientplatform",
            media_signing_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
            ),
            media_url_ttl_seconds=300,
            media_multipart_max_bytes=19_000_000,
        )
    )
    adapter = runtime.adapters.get("telegram")
    client = adapter._client  # noqa: SLF001 - composition contract
    assert client._media_gateway_origin == "https://client.example"  # noqa: SLF001
    assert client._media_gateway_path == "/clientplatform"  # noqa: SLF001
    assert client._multipart_max_bytes == 19_000_000  # noqa: SLF001
