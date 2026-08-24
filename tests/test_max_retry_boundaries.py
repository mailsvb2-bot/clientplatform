from __future__ import annotations

import asyncio
import urllib.error

import pytest

from runtime.messenger_max_sender import (
    MaxBotSender,
    MaxProviderRateLimitError,
)
from runtime.messenger_transport_errors import MessengerTransportError
from services.messenger.provider_transport import ProviderPermanentHTTPError


def test_max_media_permanent_http_error_does_not_enter_attachment_retry_loop(monkeypatch) -> None:
    calls = 0

    def fake_json_request(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ProviderPermanentHTTPError(401)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("MAX_CA_BUNDLE", raising=False)
    monkeypatch.setattr("runtime.messenger_max_sender.json_request", fake_json_request)
    monkeypatch.setattr("runtime.messenger_max_sender.asyncio.sleep", no_sleep)

    with pytest.raises(MessengerTransportError, match="HTTP 401"):
        asyncio.run(
            MaxBotSender(token="bot-token")._send_media_payload(
                "123",
                text="audio",
                media_type="audio",
                media_token="media-token",
            )
        )

    assert calls == 1


def test_max_media_network_failure_never_repeats_ambiguous_post(monkeypatch) -> None:
    calls = 0

    def fake_json_request(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("ambiguous timeout")

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("runtime.messenger_max_sender.json_request", fake_json_request)
    monkeypatch.setattr("runtime.messenger_max_sender.asyncio.sleep", no_sleep)

    with pytest.raises(TimeoutError, match="ambiguous timeout"):
        asyncio.run(
            MaxBotSender(token="bot-token")._send_media_payload(
                "123",
                text="audio",
                media_type="audio",
                media_token="media-token",
            )
        )

    assert calls == 1


def test_max_rate_limit_is_explicitly_safe_to_retry(monkeypatch) -> None:
    def fake_json_request(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://platform-api2.max.ru/messages",
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("runtime.messenger_max_sender.json_request", fake_json_request)

    with pytest.raises(MaxProviderRateLimitError) as raised:
        asyncio.run(
            MaxBotSender(token="bot-token").send_text(
                "123",
                "hello",
                legacy_ui=False,
            )
        )

    assert raised.value.retryable is True
    assert raised.value.provider_write_definitely_rejected is True
