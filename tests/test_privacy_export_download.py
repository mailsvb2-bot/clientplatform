from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from runtime import privacy_export_http
from services import privacy_export_links


def test_privacy_export_ttl_has_safe_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVACY_EXPORT_TOKEN_TTL_MINUTES", "0")
    assert privacy_export_links.privacy_export_ttl_minutes() == 2

    monkeypatch.setenv("PRIVACY_EXPORT_TOKEN_TTL_MINUTES", "999")
    assert privacy_export_links.privacy_export_ttl_minutes() == 30

    monkeypatch.setenv("PRIVACY_EXPORT_TOKEN_TTL_MINUTES", "bad")
    assert privacy_export_links.privacy_export_ttl_minutes() == 10


def test_privacy_export_token_expiry_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVACY_EXPORT_TOKEN_TTL_MINUTES", "10")
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    assert privacy_export_links._grant_expired((now - timedelta(minutes=11)).isoformat(), now=now)
    assert not privacy_export_links._grant_expired((now - timedelta(minutes=9)).isoformat(), now=now)
    assert privacy_export_links._grant_expired("not-a-date", now=now)


def test_privacy_export_requires_explicit_https_ingress_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("PRIVACY_EXPORT_HTTP_ENABLED", "1")
    monkeypatch.setenv("PRIVACY_EXPORT_PUBLIC_BASE_URL", "http://example.test")
    with pytest.raises(RuntimeError, match="valid public HTTPS"):
        privacy_export_links.privacy_export_http_enabled()

    monkeypatch.setenv("PRIVACY_EXPORT_PUBLIC_BASE_URL", "https://example.test")
    assert privacy_export_links.privacy_export_http_enabled() is True


def test_privacy_export_token_is_single_use(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVACY_EXPORT_HTTP_ENABLED", "1")
    monkeypatch.setenv("PRIVACY_EXPORT_PUBLIC_BASE_URL", "https://example.test")
    url = privacy_export_links.issue_privacy_export_url(991001, platform="telegram")
    token = urlsplit(url).path.rsplit("/", 1)[-1]

    grant = privacy_export_links.get_privacy_export_grant(token)
    assert grant is not None and grant.user_id == 991001
    claimed = privacy_export_links.claim_privacy_export_grant(token)
    assert claimed is not None and claimed.consumed_at is not None
    assert privacy_export_links.get_privacy_export_grant(token) is None
    assert privacy_export_links.claim_privacy_export_grant(token) is None


@pytest.mark.asyncio
async def test_preview_get_does_not_consume_and_post_streams_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIVACY_EXPORT_HTTP_ENABLED", "1")
    monkeypatch.setenv("PRIVACY_EXPORT_PUBLIC_BASE_URL", "https://example.test")
    url = privacy_export_links.issue_privacy_export_url(991002, platform="vk")
    path = urlsplit(url).path
    token = path.rsplit("/", 1)[-1]
    generated_paths = []

    def write_export(user_id: int, output_path):
        assert user_id == 991002
        output_path.write_bytes(b"privacy-export")
        generated_paths.append(output_path)
        return SimpleNamespace(path=output_path, compressed_size_bytes=14, total_rows=3)

    monkeypatch.setattr(privacy_export_http, "write_user_data_export_gzip", write_export)
    app = web.Application()
    app.router.add_get(f"{privacy_export_links.PRIVACY_EXPORT_PREFIX}{{token}}", privacy_export_http.privacy_export_landing)
    app.router.add_post(f"{privacy_export_links.PRIVACY_EXPORT_PREFIX}{{token}}", privacy_export_http.privacy_export_download)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        preview = await client.get(path)
        assert preview.status == 200
        assert "Скачать архив" in await preview.text()
        assert privacy_export_links.get_privacy_export_grant(token) is not None

        download = await client.post(path)
        assert download.status == 200
        assert await download.read() == b"privacy-export"
        assert download.headers["Cache-Control"].startswith("private, no-store")
        assert privacy_export_links.get_privacy_export_grant(token) is None

        replay = await client.post(path)
        assert replay.status == 404
    finally:
        await client.close()

    assert generated_paths
    assert all(not item.exists() and not item.parent.exists() for item in generated_paths)


@pytest.mark.asyncio
async def test_generation_failure_does_not_consume_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVACY_EXPORT_HTTP_ENABLED", "1")
    monkeypatch.setenv("PRIVACY_EXPORT_PUBLIC_BASE_URL", "https://example.test")
    url = privacy_export_links.issue_privacy_export_url(991003, platform="max")
    path = urlsplit(url).path
    token = path.rsplit("/", 1)[-1]

    def fail_export(*_args, **_kwargs):
        raise RuntimeError("synthetic export failure")

    monkeypatch.setattr(privacy_export_http, "write_user_data_export_gzip", fail_export)
    app = web.Application()
    app.router.add_post(f"{privacy_export_links.PRIVACY_EXPORT_PREFIX}{{token}}", privacy_export_http.privacy_export_download)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(path)
        assert response.status == 500
    finally:
        await client.close()

    assert privacy_export_links.get_privacy_export_grant(token) is not None
