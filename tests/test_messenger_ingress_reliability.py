from __future__ import annotations

import json

import pytest
from aiohttp import web

from runtime import messenger_ingress_reliability as reliability
from services.messenger.webhook_dedupe import InboundFailureResult


class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload)
        self.headers: dict[str, str] = {}
        self.query: dict[str, str] = {}

    async def text(self) -> str:
        return self._body


def _failure(*, attempts: int, retryable: bool, dead_lettered: bool) -> InboundFailureResult:
    return InboundFailureResult(
        event_key="event-1",
        attempts=attempts,
        retryable=retryable,
        dead_lettered=dead_lettered,
        recorded=True,
    )


@pytest.mark.asyncio
async def test_vk_extraction_failure_returns_503_before_dead_letter(monkeypatch) -> None:
    payload = {"type": "message_new", "object": {"message": {"id": 1}}}
    monkeypatch.setattr(reliability.base_ingress, "_vk_secret_ok", lambda _payload: True)
    monkeypatch.setattr(reliability, "extract_vk_message", lambda _payload: None)
    monkeypatch.setattr(
        reliability,
        "record_inbound_failure",
        lambda *_args, **_kwargs: _failure(attempts=1, retryable=True, dead_lettered=False),
    )

    response = await reliability.vk_webhook(FakeRequest(payload))

    assert response.status == 503
    assert response.text == "retry"


@pytest.mark.asyncio
async def test_vk_dead_letter_is_acknowledged(monkeypatch) -> None:
    payload = {"type": "message_new", "object": {"message": {"id": 2}}}
    monkeypatch.setattr(reliability.base_ingress, "_vk_secret_ok", lambda _payload: True)
    monkeypatch.setattr(reliability, "extract_vk_message", lambda _payload: None)
    monkeypatch.setattr(
        reliability,
        "record_inbound_failure",
        lambda *_args, **_kwargs: _failure(attempts=5, retryable=False, dead_lettered=True),
    )

    response = await reliability.vk_webhook(FakeRequest(payload))

    assert response.status == 200
    assert response.text == "ok"


@pytest.mark.asyncio
async def test_max_extraction_failure_exposes_retry_state(monkeypatch) -> None:
    payload = {"update_type": "message_created", "message": {"message_id": "m1"}}
    monkeypatch.setattr(reliability.base_ingress, "_max_secret_ok", lambda _request, _payload: True)
    monkeypatch.setattr(reliability, "extract_max_message", lambda _payload: None)
    monkeypatch.setattr(
        reliability,
        "record_inbound_failure",
        lambda *_args, **_kwargs: _failure(attempts=2, retryable=True, dead_lettered=False),
    )

    response = await reliability.max_webhook(FakeRequest(payload))
    body = json.loads(response.text)

    assert response.status == 503
    assert body == {"ok": False, "error": "retry", "attempts": 2, "dead_lettered": False}


@pytest.mark.asyncio
async def test_max_dead_letter_is_acknowledged(monkeypatch) -> None:
    payload = {"update_type": "message_created", "message": {"message_id": "m2"}}
    monkeypatch.setattr(reliability.base_ingress, "_max_secret_ok", lambda _request, _payload: True)
    monkeypatch.setattr(reliability, "extract_max_message", lambda _payload: None)
    monkeypatch.setattr(
        reliability,
        "record_inbound_failure",
        lambda *_args, **_kwargs: _failure(attempts=5, retryable=False, dead_lettered=True),
    )

    response = await reliability.max_webhook(FakeRequest(payload))
    body = json.loads(response.text)

    assert response.status == 200
    assert body == {"ok": True, "attempts": 5, "dead_lettered": True}


@pytest.mark.asyncio
async def test_valid_vk_payload_uses_canonical_clientplatform_entry(monkeypatch) -> None:
    payload = {"type": "message_new", "object": {"message": {"from_id": 42, "text": "start"}}}
    captured: list[dict[str, object]] = []

    monkeypatch.setattr(reliability.base_ingress, "_vk_secret_ok", lambda _payload: True)
    monkeypatch.setattr(
        reliability,
        "extract_vk_message",
        lambda _payload: {
            "user_id": 42,
            "external_user_id": "42",
            "text": "start",
            "username": None,
            "display_name": "",
            "first_name": "",
        },
    )
    monkeypatch.setattr(
        reliability,
        "_process_clientplatform_entry_and_persist",
        lambda **kwargs: captured.append(dict(kwargs)) or True,
    )

    response = await reliability.vk_webhook(FakeRequest(payload))

    assert response.status == 200
    assert response.text == "ok"
    assert captured and captured[0]["platform"] == "vk"
    assert captured[0]["text"] == "start"
