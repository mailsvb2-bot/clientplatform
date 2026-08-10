from __future__ import annotations

import io

import pytest

from services import visual_creative_gateway as gateway


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._stream = io.BytesIO(body)
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


def test_submit_rejects_gateway_scope_mismatch(monkeypatch):
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "http://gateway.internal")
    monkeypatch.setattr(
        gateway.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(
            b'{"id":"job","provider":"fake","scope_id":"other-business",'
            b'"kind":"image","status":"queued","asset_ready":false}'
        ),
    )
    with pytest.raises(gateway.VisualCreativeGatewayError, match="scope_mismatch"):
        gateway.submit_visual(
            gateway.VisualCreativeBrief(kind="image", prompt="safe creative"),
            scope_id="business-id",
            idempotency_key="clientplatform:abcdef12",
        )


def test_poll_rejects_gateway_scope_mismatch(monkeypatch):
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "http://gateway.internal")
    monkeypatch.setattr(
        gateway.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(
            b'{"id":"job","provider":"fake","scope_id":"other-business",'
            b'"kind":"image","status":"running","asset_ready":false}'
        ),
    )
    with pytest.raises(gateway.VisualCreativeGatewayError, match="scope_mismatch"):
        gateway.poll_visual("job", scope_id="business-id")
