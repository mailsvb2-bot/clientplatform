from __future__ import annotations

import io
import urllib.error
from pathlib import Path

import pytest

from services import visual_creative_gateway as gateway


class _Headers(dict[str, str]):
    def items(self):  # type: ignore[override]
        return super().items()


class _Response:
    def __init__(self, body: bytes, *, headers: dict[str, str] | None = None) -> None:
        self._stream = io.BytesIO(body)
        self.headers = _Headers(headers or {})

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _job(*, status: str = "succeeded", ready: bool = True) -> gateway.VisualCreativeJob:
    return gateway.VisualCreativeJob(
        id="job_123",
        provider="mock",
        scope_id="tenant:42",
        kind="image",
        status=status,
        model="mock-v1",
        mime_type="image/png",
        asset_ready=ready,
    )


def test_env_int_clamps_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_INT", "999")
    assert gateway._env_int("X_INT", 7, minimum=3, maximum=10) == 10
    monkeypatch.setenv("X_INT", "-99")
    assert gateway._env_int("X_INT", 7, minimum=3, maximum=10) == 3
    monkeypatch.setenv("X_INT", "nope")
    assert gateway._env_int("X_INT", 7, minimum=3, maximum=10) == 7


def test_base_url_and_headers_are_normalized_without_leaking_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "https://creative.example.test:8443/api/")
    monkeypatch.setenv("VISUAL_GATEWAY_TOKEN", " secret-token ")
    assert gateway._base_url() == "https://creative.example.test:8443/api"
    assert gateway._headers() == {
        "Accept": "application/json",
        "Authorization": "Bearer secret-token",
    }
    assert gateway._headers(json_body=True)["Content-Type"] == "application/json"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://creative.example.test",
        "https://user:pass@creative.example.test",
        "https://creative.example.test/path?debug=1",
        "https://creative.example.test/path#fragment",
        "https://creative.example.test:bad",
    ],
)
def test_base_url_rejects_unsafe_or_invalid_configuration(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("VISUAL_GATEWAY_URL", url)
    with pytest.raises(gateway.VisualCreativeGatewayError, match="visual_gateway_not_configured"):
        gateway._base_url()


def test_read_limited_accepts_small_body_and_rejects_declared_or_streamed_oversize() -> None:
    response = _Response(b"abc", headers={"Content-Length": "3"})
    assert gateway._read_limited(response, 3) == b"abc"

    with pytest.raises(gateway.VisualCreativeGatewayError, match="response_too_large"):
        gateway._read_limited(_Response(b"", headers={"Content-Length": "11"}), 10)

    with pytest.raises(gateway.VisualCreativeGatewayError, match="response_too_large"):
        gateway._read_limited(_Response(b"123456"), 5)

    assert gateway._read_limited(_Response(b"ok", headers={"Content-Length": "invalid"}), 10) == b"ok"


def test_request_builds_authenticated_json_request_and_bounds_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "https://creative.example.test/api")
    monkeypatch.setenv("VISUAL_GATEWAY_TOKEN", "token")
    monkeypatch.setenv("VISUAL_GATEWAY_TIMEOUT_SECONDS", "12")
    observed: dict[str, object] = {}

    def fake_urlopen(request, *, timeout):
        observed["url"] = request.full_url
        observed["method"] = request.get_method()
        observed["authorization"] = request.headers.get("Authorization")
        observed["content_type"] = request.headers.get("Content-type")
        observed["body"] = request.data
        observed["timeout"] = timeout
        return _Response(b'{"ok": true}', headers={"Content-Type": "application/json"})

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_urlopen)
    headers, raw = gateway._request(
        "post",
        "/v1/test",
        payload={"hello": "мир"},
        max_bytes=1024,
        timeout_seconds=5,
    )
    assert headers["content-type"] == "application/json"
    assert raw == b'{"ok": true}'
    assert observed["url"] == "https://creative.example.test/api/v1/test"
    assert observed["method"] == "POST"
    assert observed["authorization"] == "Bearer token"
    assert observed["content_type"] == "application/json"
    assert b"hello" in observed["body"]  # type: ignore[operator]
    assert observed["timeout"] == 12


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (urllib.error.URLError("down"), "visual_gateway_transport_URLError"),
        (TimeoutError("late"), "visual_gateway_transport_TimeoutError"),
        (OSError("io"), "visual_gateway_transport_OSError"),
        (ValueError("bad"), "visual_gateway_transport_ValueError"),
    ],
)
def test_request_maps_transport_failures(
    monkeypatch: pytest.MonkeyPatch, raised: BaseException, expected: str
) -> None:
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "https://creative.example.test")

    def fake_urlopen(*args, **kwargs):
        raise raised

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(gateway.VisualCreativeGatewayError, match=expected):
        gateway._request("GET", "/v1/test", max_bytes=1024)


def test_request_maps_http_error_and_tolerates_error_body_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "https://creative.example.test")

    class BrokenHttpError(urllib.error.HTTPError):
        def read(self, amt=None):  # type: ignore[override]
            raise OSError("cannot read error body")

    def fake_urlopen(*args, **kwargs):
        raise BrokenHttpError(
            "https://creative.example.test/v1/test",
            429,
            "Too Many Requests",
            {},
            None,
        )

    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(gateway.VisualCreativeGatewayError, match="visual_gateway_http_429"):
        gateway._request("GET", "/v1/test", max_bytes=1024)


def test_json_rejects_invalid_encoding_json_and_non_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "_request", lambda *a, **k: ({}, b"\xff"))
    with pytest.raises(gateway.VisualCreativeGatewayError, match="invalid_json"):
        gateway._json("GET", "/x")

    monkeypatch.setattr(gateway, "_request", lambda *a, **k: ({}, b"not-json"))
    with pytest.raises(gateway.VisualCreativeGatewayError, match="invalid_json"):
        gateway._json("GET", "/x")

    monkeypatch.setattr(gateway, "_request", lambda *a, **k: ({}, b"[]"))
    with pytest.raises(gateway.VisualCreativeGatewayError, match="invalid_response"):
        gateway._json("GET", "/x")

    monkeypatch.setattr(gateway, "_request", lambda *a, **k: ({}, b""))
    assert gateway._json("GET", "/x") == {}


def test_job_parser_and_done_contract() -> None:
    parsed = gateway._job(
        {
            "id": "job_1",
            "provider": "p",
            "scope_id": "tenant:1",
            "kind": "image",
            "status": "running",
            "asset_ready": False,
        }
    )
    assert parsed.done is False
    assert gateway.VisualCreativeJob("j", "p", "s", "image", "failed").done is True
    with pytest.raises(gateway.VisualCreativeGatewayError, match="invalid_job"):
        gateway._job({"id": "bad id", "scope_id": "tenant:1", "kind": "image", "status": "queued"})
    with pytest.raises(gateway.VisualCreativeGatewayError, match="scope_mismatch"):
        gateway._require_scope(parsed, expected_scope="tenant:other")


def test_submit_visual_normalizes_payload_and_checks_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_json(method, path, *, payload=None, timeout_seconds=None):
        observed.update(method=method, path=path, payload=payload, timeout=timeout_seconds)
        return {
            "id": "job_1",
            "provider": "provider",
            "scope_id": "tenant:42",
            "kind": "image",
            "status": "queued",
        }

    monkeypatch.setattr(gateway, "_json", fake_json)
    job = gateway.submit_visual(
        gateway.VisualCreativeBrief(
            kind=" IMAGE ",
            prompt="  Create a calm visual  ",
            duration_seconds=99,
            aspect_ratio="4:5",
            seed=7,
        ),
        scope_id="tenant:42",
        idempotency_key="creative:test:123",
        wait_seconds=99,
    )
    payload = observed["payload"]
    assert isinstance(payload, dict)
    assert job.status == "queued"
    assert payload["kind"] == "image"
    assert payload["prompt"] == "Create a calm visual"
    assert payload["duration_seconds"] == 15
    assert payload["wait_seconds"] == 60
    assert observed["timeout"] == 75

    with pytest.raises(ValueError, match="valid visual kind"):
        gateway.submit_visual(
            gateway.VisualCreativeBrief(kind="document", prompt="x"),
            scope_id="tenant:42",
            idempotency_key="creative:test:123",
        )
    with pytest.raises(ValueError, match="valid visual scope"):
        gateway.submit_visual(
            gateway.VisualCreativeBrief(kind="image", prompt="x"),
            scope_id="bad scope",
            idempotency_key="short",
        )

    monkeypatch.setattr(
        gateway,
        "_json",
        lambda *a, **k: {
            "id": "job_2",
            "provider": "provider",
            "scope_id": "tenant:other",
            "kind": "image",
            "status": "queued",
        },
    )
    with pytest.raises(gateway.VisualCreativeGatewayError, match="scope_mismatch"):
        gateway.submit_visual(
            gateway.VisualCreativeBrief(kind="image", prompt="x"),
            scope_id="tenant:42",
            idempotency_key="creative:test:456",
        )


def test_poll_visual_validates_and_encodes_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, str] = {}

    def fake_json(method, path, **kwargs):
        observed["method"] = method
        observed["path"] = path
        return {
            "id": "job_1",
            "provider": "provider",
            "scope_id": "tenant:42",
            "kind": "image",
            "status": "succeeded",
            "asset_ready": True,
        }

    monkeypatch.setattr(gateway, "_json", fake_json)
    result = gateway.poll_visual("job_1", scope_id="tenant:42")
    assert result.done is True
    assert observed["method"] == "GET"
    assert "scope_id=tenant%3A42" in observed["path"]
    with pytest.raises(ValueError, match="job id"):
        gateway.poll_visual("bad job", scope_id="tenant:42")
    with pytest.raises(ValueError, match="scope"):
        gateway.poll_visual("job_1", scope_id="bad scope")


def test_wait_visual_returns_terminal_or_polls_until_done(monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = _job()
    assert gateway.wait_visual(terminal, wait_seconds=20) is terminal

    running = _job(status="running", ready=False)
    clock = iter((0.0, 0.0, 2.0))
    sleeps: list[float] = []
    monkeypatch.setattr(gateway.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(gateway.time, "sleep", lambda value: sleeps.append(value))
    monkeypatch.setattr(gateway, "poll_visual", lambda *a, **k: terminal)
    result = gateway.wait_visual(running, wait_seconds=1, poll_interval=0.01)
    assert result is terminal
    assert sleeps == [0.2]
    assert gateway.wait_visual(running, wait_seconds=0) is running


def test_download_visual_materializes_verified_media(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        gateway,
        "_request",
        lambda *a, **k: ({"content-type": "image/jpeg; charset=binary"}, b"jpeg-bytes"),
    )
    path = gateway.download_visual(_job(), output_dir=str(tmp_path))
    assert path.name == "image-job_123.jpg"
    assert path.read_bytes() == b"jpeg-bytes"
    assert not path.with_suffix(path.suffix + ".tmp").exists()

    with pytest.raises(gateway.VisualCreativeGatewayError, match="content_not_ready"):
        gateway.download_visual(_job(status="running", ready=False), output_dir=str(tmp_path))

    monkeypatch.setattr(gateway, "_request", lambda *a, **k: ({"content-type": "video/mp4"}, b"x"))
    with pytest.raises(gateway.VisualCreativeGatewayError, match="unexpected_media_type"):
        gateway.download_visual(_job(), output_dir=str(tmp_path))


def test_download_visual_rejects_invalid_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    invalid = gateway.VisualCreativeJob(
        id="job_123",
        provider="mock",
        scope_id="bad scope",
        kind="image",
        status="succeeded",
        asset_ready=True,
    )
    with pytest.raises(gateway.VisualCreativeGatewayError, match="invalid_scope"):
        gateway.download_visual(invalid, output_dir=str(tmp_path))


def test_gateway_snapshot_exposes_only_safe_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "https://user:pass@creative.example.test:9443/private/path")
    monkeypatch.setenv("VISUAL_GATEWAY_TOKEN", "top-secret")
    snapshot = gateway.gateway_snapshot()
    assert snapshot == {
        "configured": True,
        "base_url": "https://creative.example.test:9443",
        "token_configured": True,
    }
    assert "user" not in snapshot["base_url"]
    assert "pass" not in snapshot["base_url"]
    assert "top-secret" not in str(snapshot)

    monkeypatch.setenv("VISUAL_GATEWAY_URL", "https://creative.example.test:bad")
    monkeypatch.delenv("VISUAL_GATEWAY_TOKEN", raising=False)
    assert gateway.gateway_snapshot() == {
        "configured": False,
        "base_url": "",
        "token_configured": False,
    }
