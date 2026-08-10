from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from services import visual_creative_gateway as gateway


class FakeResponse:
    def __init__(self, body: bytes, *, content_type: str = "application/json") -> None:
        self._stream = io.BytesIO(body)
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


def test_submit_builds_scoped_gateway_request(monkeypatch):
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "http://gateway.internal:8097")
    monkeypatch.setenv("VISUAL_GATEWAY_TOKEN", "secret")
    seen = {}

    def fake_open(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.headers.get("Authorization")
        seen["payload"] = json.loads((request.data or b"{}").decode())
        seen["timeout"] = timeout
        return FakeResponse(
            b'{"id":"j1","provider":"yandexart","scope_id":"tenant-1",'
            b'"kind":"image","status":"queued","asset_ready":false}'
        )

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_open)
    job = gateway.submit_visual(
        gateway.VisualCreativeBrief(
            kind="image",
            prompt="calm city",
            country_code="RU",
        ),
        scope_id="tenant-1",
        idempotency_key="request-0001",
        wait_seconds=7,
    )
    assert job.id == "j1"
    assert job.scope_id == "tenant-1"
    assert seen["url"].endswith("/v1/creative/generations")
    assert seen["auth"] == "Bearer secret"
    assert seen["payload"]["country_code"] == "RU"
    assert seen["payload"]["wait_seconds"] == 7
    assert seen["payload"]["scope_id"] == "tenant-1"
    assert seen["payload"]["idempotency_key"] == "request-0001"
    assert seen["timeout"] == 30


def test_invalid_gateway_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("VISUAL_GATEWAY_URL", raising=False)
    with pytest.raises(gateway.VisualCreativeGatewayError, match="not_configured"):
        gateway.poll_visual("job", scope_id="tenant-1")


def test_base_url_rejects_embedded_credentials(monkeypatch):
    monkeypatch.setenv(
        "VISUAL_GATEWAY_URL",
        "https://user:secret@gateway.internal/private?token=x",
    )
    with pytest.raises(gateway.VisualCreativeGatewayError, match="not_configured"):
        gateway._base_url()


def test_snapshot_never_exposes_gateway_credentials(monkeypatch):
    monkeypatch.setenv(
        "VISUAL_GATEWAY_URL",
        "https://user:pass@gateway.internal:8443/private?token=x",
    )
    monkeypatch.setenv("VISUAL_GATEWAY_TOKEN", "super-secret")
    snap = gateway.gateway_snapshot()
    rendered = repr(snap)
    assert snap["base_url"] == "https://gateway.internal:8443"
    assert snap["token_configured"] is True
    assert "user" not in rendered
    assert "pass" not in rendered
    assert "token=x" not in rendered
    assert "super-secret" not in rendered


def test_wait_polls_with_original_scope_until_done(monkeypatch):
    sequence = [
        gateway.VisualCreativeJob(
            id="j",
            provider="x",
            scope_id="tenant-1",
            kind="video",
            status="running",
        ),
        gateway.VisualCreativeJob(
            id="j",
            provider="x",
            scope_id="tenant-1",
            kind="video",
            status="succeeded",
            asset_ready=True,
        ),
    ]
    scopes = []
    monkeypatch.setattr(gateway.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        gateway.time,
        "monotonic",
        iter([0.0, 0.1, 0.2, 0.3]).__next__,
    )

    def fake_poll(_job_id, *, scope_id):
        scopes.append(scope_id)
        return sequence.pop(0)

    monkeypatch.setattr(gateway, "poll_visual", fake_poll)
    start = gateway.VisualCreativeJob(
        id="j",
        provider="x",
        scope_id="tenant-1",
        kind="video",
        status="queued",
    )
    result = gateway.wait_visual(start, wait_seconds=1, poll_interval=0.2)
    assert result.status == "succeeded"
    assert scopes == ["tenant-1", "tenant-1"]


def test_download_is_bounded_scoped_and_materialized(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "http://gateway.internal")
    body = b"video-bytes"
    seen = {}

    def fake_open(request, **_kwargs):
        seen["url"] = request.full_url
        return FakeResponse(body, content_type="video/mp4")

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_open)
    job = gateway.VisualCreativeJob(
        id="j2",
        provider="x",
        scope_id="tenant-1",
        kind="video",
        status="succeeded",
        asset_ready=True,
    )
    path = gateway.download_visual(job, output_dir=str(tmp_path))
    assert path.read_bytes() == body
    assert path.suffix == ".mp4"
    assert "scope_id=tenant-1" in seen["url"]


def test_download_rejects_wrong_mime(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "http://gateway.internal")
    monkeypatch.setattr(
        gateway.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"x", content_type="text/html"),
    )
    job = gateway.VisualCreativeJob(
        id="j3",
        provider="x",
        scope_id="tenant-1",
        kind="image",
        status="succeeded",
        asset_ready=True,
    )
    with pytest.raises(gateway.VisualCreativeGatewayError, match="unexpected_media_type"):
        gateway.download_visual(job, output_dir=str(tmp_path))


def test_job_rejects_unsafe_identifier_and_scope():
    with pytest.raises(gateway.VisualCreativeGatewayError, match="invalid_job"):
        gateway._job(
            {
                "id": "../escape",
                "scope_id": "tenant-1",
                "kind": "image",
                "status": "succeeded",
                "asset_ready": True,
            }
        )
    with pytest.raises(gateway.VisualCreativeGatewayError, match="invalid_job"):
        gateway._job(
            {
                "id": "safe",
                "scope_id": "../tenant?secret=x",
                "kind": "image",
                "status": "queued",
            }
        )


def test_submit_rejects_invalid_scope_and_idempotency_before_network(monkeypatch):
    called = False

    def fake_open(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be called")

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_open)
    brief = gateway.VisualCreativeBrief(kind="image", prompt="calm city")
    with pytest.raises(ValueError, match="scope and idempotency"):
        gateway.submit_visual(
            brief,
            scope_id="../tenant?x=1",
            idempotency_key="short",
        )
    assert called is False


def test_submit_timeout_covers_gateway_wait(monkeypatch):
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "http://gateway.internal:8097")
    monkeypatch.setenv("VISUAL_GATEWAY_TIMEOUT_SECONDS", "30")
    seen = {}

    def fake_open(_request, timeout):
        seen["timeout"] = timeout
        return FakeResponse(
            b'{"id":"j4","provider":"x","scope_id":"tenant-1",'
            b'"kind":"video","status":"queued","asset_ready":false}'
        )

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_open)
    gateway.submit_visual(
        gateway.VisualCreativeBrief(kind="video", prompt="rain"),
        scope_id="tenant-1",
        idempotency_key="request-0004",
        wait_seconds=60,
    )
    assert seen["timeout"] >= 75


def test_poll_rejects_unsafe_input_before_request(monkeypatch):
    called = False

    def fake_open(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be called")

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_open)
    with pytest.raises(ValueError, match="valid visual job id"):
        gateway.poll_visual("../escape", scope_id="tenant-1")
    with pytest.raises(ValueError, match="valid visual scope"):
        gateway.poll_visual("safe", scope_id="../tenant?x=1")
    assert called is False


def test_poll_includes_scope_id(monkeypatch):
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "http://gateway.internal")
    seen = {}

    def fake_open(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return FakeResponse(
            b'{"id":"j5","provider":"x","scope_id":"tenant-1",'
            b'"kind":"image","status":"queued","asset_ready":false}'
        )

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_open)
    job = gateway.poll_visual("j5", scope_id="tenant-1")
    assert "scope_id=tenant-1" in seen["url"]
    assert seen["timeout"] == 30
    assert job.scope_id == "tenant-1"
