from __future__ import annotations

import io
import urllib.error

import pytest

import services.visual_creative_gateway as gateway
from services.visual_creative_gateway import VisualCreativeGatewayError


class _Response:
    def __init__(self, body: bytes, *, content_length: str = "") -> None:
        self.headers = {"Content-Length": content_length} if content_length else {}
        self._stream = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def test_env_int_bounds_and_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VCG_TEST_INT", "not-an-int")
    assert gateway._env_int("VCG_TEST_INT", 17, minimum=3, maximum=30) == 17

    monkeypatch.setenv("VCG_TEST_INT", "1")
    assert gateway._env_int("VCG_TEST_INT", 17, minimum=3, maximum=30) == 3

    monkeypatch.setenv("VCG_TEST_INT", "99")
    assert gateway._env_int("VCG_TEST_INT", 17, minimum=3, maximum=30) == 30


def test_base_url_and_headers_validate_operator_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISUAL_GATEWAY_URL", "https://gateway.example:8443/api/")
    monkeypatch.setenv("VISUAL_GATEWAY_TOKEN", " secret-token ")
    assert gateway._base_url() == "https://gateway.example:8443/api"
    assert gateway._headers() == {
        "Accept": "application/json",
        "Authorization": "Bearer secret-token",
    }
    assert gateway._headers(json_body=True)["Content-Type"] == "application/json"

    monkeypatch.delenv("VISUAL_GATEWAY_TOKEN", raising=False)
    assert "Authorization" not in gateway._headers()

    for invalid in (
        "",
        "ftp://gateway.example/api",
        "https://user:pass@gateway.example/api",
        "https://gateway.example/api?debug=1",
        "https://gateway.example/api#fragment",
        "https://gateway.example:bad/api",
    ):
        monkeypatch.setenv("VISUAL_GATEWAY_URL", invalid)
        with pytest.raises(VisualCreativeGatewayError, match="not_configured"):
            gateway._base_url()


def test_read_limited_rejects_declared_and_streamed_oversize() -> None:
    with pytest.raises(VisualCreativeGatewayError, match="response_too_large"):
        gateway._read_limited(_Response(b"small", content_length="999"), 10)

    assert gateway._read_limited(_Response(b"abc", content_length="not-a-number"), 10) == b"abc"
    assert gateway._read_limited(_Response(b"abcdef"), 6) == b"abcdef"

    with pytest.raises(VisualCreativeGatewayError, match="response_too_large"):
        gateway._read_limited(_Response(b"abcdefg"), 6)


def test_json_rejects_invalid_utf8_invalid_json_and_non_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def response(raw: bytes):
        monkeypatch.setattr(gateway, "_request", lambda *a, **k: ({}, raw))
        return gateway._json("GET", "/probe")

    with pytest.raises(VisualCreativeGatewayError, match="invalid_json"):
        response(b"\xff")
    with pytest.raises(VisualCreativeGatewayError, match="invalid_json"):
        response(b"{")
    with pytest.raises(VisualCreativeGatewayError, match="invalid_response"):
        response(b"[]")
    assert response(b"") == {}
    assert response(b'{"ok": true}') == {"ok": True}


def test_request_maps_http_and_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway, "_base_url", lambda: "https://gateway.example")

    http_error = urllib.error.HTTPError(
        "https://gateway.example/probe",
        429,
        "too many requests",
        hdrs=None,
        fp=io.BytesIO(b"provider details"),
    )
    monkeypatch.setattr(gateway.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(http_error))
    with pytest.raises(VisualCreativeGatewayError, match="http_429"):
        gateway._request("GET", "/probe", max_bytes=1024)

    failures = (
        urllib.error.URLError("offline"),
        TimeoutError("slow"),
        OSError("socket"),
        ValueError("bad url"),
    )
    for failure in failures:
        monkeypatch.setattr(
            gateway.urllib.request,
            "urlopen",
            lambda *a, _failure=failure, **k: (_ for _ in ()).throw(_failure),
        )
        with pytest.raises(VisualCreativeGatewayError, match="visual_gateway_transport_"):
            gateway._request("GET", "/probe", max_bytes=1024)


def test_request_success_normalizes_headers_and_honors_timeout_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ContextResponse(_Response):
        def __init__(self) -> None:
            super().__init__(b"payload", content_length="7")
            self.headers = {"Content-Length": "7", "Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    observed: dict[str, object] = {}

    def fake_urlopen(request, *, timeout):
        observed["url"] = request.full_url
        observed["timeout"] = timeout
        return _ContextResponse()

    monkeypatch.setenv("VISUAL_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setenv("VISUAL_GATEWAY_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr(gateway.urllib.request, "urlopen", fake_urlopen)

    headers, raw = gateway._request(
        "POST",
        "/probe",
        payload={"hello": "world"},
        max_bytes=1024,
        timeout_seconds=5,
    )
    assert observed == {"url": "https://gateway.example/probe", "timeout": 30}
    assert headers["content-type"] == "application/json"
    assert raw == b"payload"


def test_job_and_scope_contract_reject_malformed_provider_payloads() -> None:
    valid = {
        "id": "job-1",
        "provider": "fake",
        "scope_id": "business-a",
        "kind": "image",
        "status": "running",
        "asset_ready": False,
    }
    job = gateway._job(valid)
    assert gateway._require_scope(job, expected_scope="business-a") is job

    with pytest.raises(VisualCreativeGatewayError, match="scope_mismatch"):
        gateway._require_scope(job, expected_scope="business-b")

    for patch in (
        {"id": "bad id"},
        {"scope_id": "bad scope!"},
        {"kind": "audio"},
        {"status": "mystery"},
    ):
        payload = dict(valid)
        payload.update(patch)
        with pytest.raises(VisualCreativeGatewayError, match="invalid_job"):
            gateway._job(payload)
