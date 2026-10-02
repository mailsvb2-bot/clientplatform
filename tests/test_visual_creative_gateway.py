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



def test_configured_visual_providers_uses_gateway_capability_snapshot(monkeypatch):
    seen = {}

    def fake_json(method, path, **_kwargs):
        seen["method"] = method
        seen["path"] = path
        return {
            "enabled": True,
            "configured_image": ["yandexart", "gigachat"],
            "configured_video": ["yandexart_motion"],
        }

    monkeypatch.setattr(gateway, "_json", fake_json)
    assert gateway.configured_visual_providers("image", country_code="RU") == (
        "yandexart",
        "gigachat",
    )
    assert gateway.configured_visual_providers("video", country_code="RU") == (
        "yandexart_motion",
    )
    assert seen["method"] == "GET"
    assert "country_code=RU" in seen["path"]


def test_configured_visual_video_mode_reads_explicit_snapshot(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "_json",
        lambda *_args, **_kwargs: {
            "enabled": True,
            "configured_video": ["runway", "yandexart_motion"],
            "video_generation_mode": "native",
        },
    )
    assert gateway.configured_visual_video_mode(country_code="RU") == "native"


def test_configured_visual_video_mode_supports_rolling_old_upstream(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "_json",
        lambda *_args, **_kwargs: {
            "enabled": True,
            "configured_video": ["yandexart_motion"],
        },
    )
    assert gateway.configured_visual_video_mode(country_code="RU") == "motion"


def test_configured_visual_providers_fails_closed_when_generation_disabled(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "_json",
        lambda *_args, **_kwargs: {
            "enabled": False,
            "configured_image": ["yandexart"],
            "configured_video": ["yandexart_motion"],
        },
    )
    assert gateway.configured_visual_providers("image", country_code="RU") == ()


def test_configured_visual_providers_rejects_malformed_snapshot(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "_json",
        lambda *_args, **_kwargs: {
            "enabled": True,
            "configured_image": ["../../secret"],
            "configured_video": [],
        },
    )
    with pytest.raises(
        gateway.VisualCreativeGatewayError,
        match="invalid_provider_snapshot",
    ):
        gateway.configured_visual_providers("image", country_code="RU")


def test_configured_visual_providers_rejects_unsupported_kind():
    with pytest.raises(ValueError, match="image or video"):
        gateway.configured_visual_providers("audio")



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


def test_download_retries_transient_content_gap_without_resubmitting(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "http://gateway.internal")
    attempts = []
    sleeps = []
    clock = {"now": 0.0}

    def fake_request(method, path, **kwargs):
        attempts.append((method, path, kwargs.get("timeout_seconds")))
        if len(attempts) < 3:
            raise gateway.VisualCreativeGatewayError("visual_gateway_http_404")
        return ({"content-type": "image/jpeg"}, b"\xff\xd8\xfffresh")

    def fake_sleep(delay):
        sleeps.append(delay)
        clock["now"] += delay

    monkeypatch.setattr(gateway, "_request", fake_request)
    monkeypatch.setattr(gateway.time, "sleep", fake_sleep)
    monkeypatch.setattr(gateway.time, "monotonic", lambda: clock["now"])
    job = gateway.VisualCreativeJob(
        id="fresh-image",
        provider="yandexart",
        scope_id="tenant-1",
        kind="image",
        status="succeeded",
        asset_ready=True,
    )

    path = gateway.download_visual(job, output_dir=str(tmp_path))

    assert path.read_bytes() == b"\xff\xd8\xfffresh"
    assert len(attempts) == 3
    assert all(method == "GET" for method, _path, _timeout in attempts)
    assert len({path for _method, path, _timeout in attempts}) == 1
    assert all(timeout == 60 for _method, _path, timeout in attempts)
    assert sleeps == pytest.approx(
        [
            gateway._content_retry_delay("fresh-image", 0),
            gateway._content_retry_delay("fresh-image", 1),
        ]
    )


def test_download_honors_retry_after_without_new_paid_submit(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "http://gateway.internal")
    attempts = []
    sleeps = []
    clock = {"now": 0.0}

    def fake_request(method, path, **_kwargs):
        attempts.append((method, path))
        if len(attempts) == 1:
            raise gateway.VisualCreativeGatewayError(
                "visual_gateway_http_429",
                retry_after_seconds=5.0,
            )
        return ({"content-type": "image/jpeg"}, b"\xff\xd8\xfffresh")

    def fake_sleep(delay):
        sleeps.append(delay)
        clock["now"] += delay

    monkeypatch.setattr(gateway, "_request", fake_request)
    monkeypatch.setattr(gateway.time, "sleep", fake_sleep)
    monkeypatch.setattr(gateway.time, "monotonic", lambda: clock["now"])
    job = gateway.VisualCreativeJob(
        id="rate-limited-image",
        provider="yandexart",
        scope_id="tenant-1",
        kind="image",
        status="succeeded",
        asset_ready=True,
    )

    path = gateway.download_visual(job, output_dir=str(tmp_path))

    assert path.read_bytes() == b"\xff\xd8\xfffresh"
    assert attempts == [
        ("GET", attempts[0][1]),
        ("GET", attempts[0][1]),
    ]
    assert sleeps == pytest.approx([5.0])


def test_download_stops_transient_retries_at_kind_deadline(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "http://gateway.internal")
    monkeypatch.setenv("VISUAL_CONTENT_RECOVERY_IMAGE_SECONDS", "5")
    attempts = []
    sleeps = []
    clock = {"now": 0.0}

    def fake_request(method, path, **_kwargs):
        attempts.append((method, path))
        raise gateway.VisualCreativeGatewayError("visual_gateway_http_503")

    def fake_sleep(delay):
        sleeps.append(delay)
        clock["now"] += delay

    monkeypatch.setattr(gateway, "_request", fake_request)
    monkeypatch.setattr(gateway.time, "sleep", fake_sleep)
    monkeypatch.setattr(gateway.time, "monotonic", lambda: clock["now"])
    job = gateway.VisualCreativeJob(
        id="deadline-image",
        provider="yandexart",
        scope_id="tenant-1",
        kind="image",
        status="succeeded",
        asset_ready=True,
    )

    with pytest.raises(
        gateway.VisualCreativeGatewayError,
        match="visual_gateway_http_503",
    ):
        gateway.download_visual(job, output_dir=str(tmp_path))

    assert len(attempts) >= 2
    assert all(method == "GET" for method, _path in attempts)
    assert len({path for _method, path in attempts}) == 1
    assert sum(sleeps) < 5.0
    assert clock["now"] < 5.0


def test_download_uses_kind_specific_transfer_timeout(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "http://gateway.internal")
    timeouts = []

    def fake_request(_method, _path, **kwargs):
        timeouts.append(kwargs.get("timeout_seconds"))
        return ({"content-type": "application/octet-stream"}, b"payload")

    monkeypatch.setattr(gateway, "_request", fake_request)
    image = gateway.VisualCreativeJob(
        id="image-timeout",
        provider="yandexart",
        scope_id="tenant-1",
        kind="image",
        status="succeeded",
        asset_ready=True,
    )
    video = gateway.VisualCreativeJob(
        id="video-timeout",
        provider="yandexart_motion",
        scope_id="tenant-1",
        kind="video",
        status="succeeded",
        asset_ready=True,
    )

    gateway.download_visual(image, output_dir=str(tmp_path))
    gateway.download_visual(video, output_dir=str(tmp_path))

    assert timeouts == [60, 180]


def test_download_does_not_retry_permanent_content_contract_error(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "http://gateway.internal")
    attempts = []

    def fake_request(*_args, **_kwargs):
        attempts.append(1)
        raise gateway.VisualCreativeGatewayError("visual_gateway_http_401")

    monkeypatch.setattr(gateway, "_request", fake_request)
    monkeypatch.setattr(
        gateway.time,
        "sleep",
        lambda _delay: pytest.fail("permanent errors must not be retried"),
    )
    job = gateway.VisualCreativeJob(
        id="forbidden-image",
        provider="yandexart",
        scope_id="tenant-1",
        kind="image",
        status="succeeded",
        asset_ready=True,
    )

    with pytest.raises(gateway.VisualCreativeGatewayError, match="visual_gateway_http_401"):
        gateway.download_visual(job, output_dir=str(tmp_path))

    assert attempts == [1]


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