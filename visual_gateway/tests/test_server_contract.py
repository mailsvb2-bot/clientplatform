from __future__ import annotations

import asyncio
import hashlib
import io
import os
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from clientplatform.domain.visual_typography import VISUAL_TYPOGRAPHY_PRESETS
from visual_gateway.server import (
    FONT_PRESETS,
    FORMATS,
    GatewayConfig,
    GatewayError,
    Store,
    _font_preset,
    _render_request,
    create_app,
)

TOKEN = "test-gateway-token"
UPSTREAM_TOKEN = "test-upstream-token"


def _image() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (800, 600), "#8899AA").save(out, format="JPEG")
    return out.getvalue()


def test_transient_render_cleanup_expires_bytes_and_readiness(tmp_path):
    state = tmp_path / "state"
    transient = tmp_path / "transient"
    store = Store(state, asset_root=transient, asset_ttl_seconds=300)
    pack_id, _ = store.get_or_create_pack(
        scope_id="tenant-a",
        source_job_id="job1",
        idempotency_key="tenant-a:render:cleanup",
        request_hash="a" * 64,
        formats=["feed"],
        composition={},
    )
    claim = store.claim(pack_id)
    assert claim
    pack_dir = transient / pack_id
    pack_dir.mkdir(parents=True)
    target = pack_dir / "feed.jpg"
    target.write_bytes(_image())
    old = time.time() - 601
    os.utime(target, (old, old))
    store.succeed(
        pack_id,
        claim,
        [{
            "format_id": "feed",
            "kind": "image",
            "width": 1080,
            "height": 1350,
            "mime_type": "image/jpeg",
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "path": f"{pack_id}/feed.jpg",
        }],
    )

    assert store.pack(pack_id, "tenant-a")["assets"][0]["asset_ready"] is True
    assert store.cleanup_transient_assets() == 1
    assert store.pack(pack_id, "tenant-a")["assets"][0]["asset_ready"] is False
    assert not target.exists()


def test_transient_render_output_requirement_rejects_persistent_directory(tmp_path, monkeypatch):
    persistent = tmp_path / "persistent-assets"
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "ram-root"))
    config = GatewayConfig(
        token=TOKEN,
        upstream_url="http://127.0.0.1:9999",
        upstream_token="",
        state_dir=tmp_path / "state",
        asset_dir=persistent,
        transient_assets_required=True,
    )

    with pytest.raises(RuntimeError, match="persistent_user_media_forbidden"):
        create_app(config)


@pytest.fixture
async def aiohttp_client():
    clients: list[TestClient] = []

    async def factory(app: web.Application) -> TestClient:
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield factory
    for client in reversed(clients):
        await client.close()


@pytest.fixture
async def upstream(aiohttp_client):
    calls = {"post": 0, "content": 0, "semantic_qa": 0}
    source = _image()

    async def create(request):
        assert request.headers["Authorization"] == f"Bearer {UPSTREAM_TOKEN}"
        calls["post"] += 1
        payload = await request.json()
        return web.json_response(
            {
                "id": "job1",
                "provider": "fake",
                "scope_id": payload["scope_id"],
                "kind": "image",
                "status": "succeeded",
                "mime_type": "image/jpeg",
                "asset_ready": True,
            }
        )

    async def get_job(request):
        return web.json_response(
            {
                "id": request.match_info["job_id"],
                "provider": "fake",
                "scope_id": request.query["scope_id"],
                "kind": "image",
                "status": "succeeded",
                "mime_type": "image/jpeg",
                "asset_ready": True,
            }
        )

    async def content(_):
        calls["content"] += 1
        return web.Response(body=source, content_type="image/jpeg")

    async def semantic_qa(request):
        calls["semantic_qa"] += 1
        payload = await request.json()
        assert payload["scope_id"] == "tenant-a"
        assert isinstance(payload["contract"], dict)
        return web.json_response(
            {
                "status": "needs_review",
                "issues": ["Запрошенное изменение визуально не читается."],
                "summary": "Нужна проверка владельцем.",
            }
        )

    async def providers(_):
        return web.json_response({"providers": ["fake"]})

    async def usage(_):
        return web.json_response({"usage": {}})

    app = web.Application()
    app.router.add_post("/v1/creative/generations", create)
    app.router.add_get("/v1/creative/generations/{job_id}", get_job)
    app.router.add_get("/v1/creative/generations/{job_id}/content", content)
    app.router.add_post(
        "/v1/creative/generations/{job_id}/semantic-qa",
        semantic_qa,
    )
    app.router.add_get("/v1/providers", providers)
    app.router.add_get("/v1/usage", usage)
    client = await aiohttp_client(app)
    client.calls = calls
    return client


@pytest.fixture
async def gateway(aiohttp_client, upstream, tmp_path):
    config = GatewayConfig(
        token=TOKEN,
        upstream_url=str(upstream.make_url("")).rstrip("/"),
        upstream_token=UPSTREAM_TOKEN,
        state_dir=tmp_path / "state",
        daily_generation_limit=1,
        font_path="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    return await aiohttp_client(create_app(config))


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.asyncio
async def test_generation_content_proxy_reads_complete_multi_chunk_body(
    aiohttp_client,
    tmp_path,
):
    source = (
        b"a" * (64 * 1024)
        + b"b" * (64 * 1024)
        + b"c" * 12_345
    )

    async def content(request):
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "image/jpeg"},
        )
        await response.prepare(request)
        await response.write(source[: 64 * 1024])
        await asyncio.sleep(0)
        await response.write(source[64 * 1024 : 128 * 1024])
        await asyncio.sleep(0)
        await response.write(source[128 * 1024 :])
        await response.write_eof()
        return response

    upstream_app = web.Application()
    upstream_app.router.add_get(
        "/v1/creative/generations/{job_id}/content",
        content,
    )
    upstream = await aiohttp_client(upstream_app)
    client = await aiohttp_client(
        create_app(
            GatewayConfig(
                token=TOKEN,
                upstream_url=str(upstream.make_url("")).rstrip("/"),
                upstream_token="",
                state_dir=tmp_path / "state",
            )
        )
    )

    response = await client.get(
        "/v1/creative/generations/job1/content?scope_id=tenant-a",
        headers=auth(),
    )
    raw = await response.read()

    assert response.status == 200
    assert len(raw) == len(source)
    assert raw == source


@pytest.mark.asyncio
async def test_generation_content_proxy_enforces_cumulative_stream_limit(
    aiohttp_client,
    tmp_path,
):
    source = b"a" * (64 * 1024) + b"b" * (64 * 1024)

    async def content(request):
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "image/jpeg"},
        )
        await response.prepare(request)
        await response.write(source[: 64 * 1024])
        await asyncio.sleep(0)
        await response.write(source[64 * 1024 :])
        await response.write_eof()
        return response

    upstream_app = web.Application()
    upstream_app.router.add_get(
        "/v1/creative/generations/{job_id}/content",
        content,
    )
    upstream = await aiohttp_client(upstream_app)
    client = await aiohttp_client(
        create_app(
            GatewayConfig(
                token=TOKEN,
                upstream_url=str(upstream.make_url("")).rstrip("/"),
                upstream_token="",
                state_dir=tmp_path / "state",
                max_source_bytes=96 * 1024,
            )
        )
    )

    response = await client.get(
        "/v1/creative/generations/job1/content?scope_id=tenant-a",
        headers=auth(),
    )

    assert response.status == 502
    assert (await response.json())["error_code"] == "provider_gateway_source_too_large"


@pytest.mark.asyncio
async def test_generation_content_proxy_uses_idle_timeout_not_total_transfer_timeout(
    aiohttp_client,
    tmp_path,
):
    source = b"a" * 4096 + b"b" * 4096

    async def content(request):
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "image/jpeg"},
        )
        await response.prepare(request)
        await response.write(source[:4096])
        await asyncio.sleep(0.15)
        await response.write(source[4096:])
        await response.write_eof()
        return response

    upstream_app = web.Application()
    upstream_app.router.add_get(
        "/v1/creative/generations/{job_id}/content",
        content,
    )
    upstream = await aiohttp_client(upstream_app)
    client = await aiohttp_client(
        create_app(
            GatewayConfig(
                token=TOKEN,
                upstream_url=str(upstream.make_url("")).rstrip("/"),
                upstream_token="",
                state_dir=tmp_path / "state",
                upstream_timeout_seconds=0.10,
                upstream_media_idle_timeout_seconds=1,
            )
        )
    )

    response = await client.get(
        "/v1/creative/generations/job1/content?scope_id=tenant-a",
        headers=auth(),
    )
    raw = await response.read()

    assert response.status == 200
    assert raw == source





@pytest.mark.asyncio
async def test_semantic_qa_is_proxied_through_canonical_visual_gateway(gateway, upstream):
    payload = {
        "scope_id": "tenant-a",
        "contract": {
            "version": 2,
            "kind": "image",
            "country_code": "RU",
            "owner_request": "тот же объект постепенно меняет визуальный стиль",
            "semantic_flags": [
                "presentation_change",
                "presentation_transition",
            ],
            "scene_contract": {
                "version": 1,
                "topology": "static",
                "primary_subject": "объект",
                "initial_state": [],
                "actions": [],
                "cause": "",
                "transition": [],
                "final_state": [],
                "explicit_text": [],
                "required_evidence": [
                    "visual presentation transition readable within the same scene"
                ],
                "forbidden": [
                    "physical mutation caused only by presentation or style wording"
                ],
            },
        },
    }

    response = await gateway.post(
        "/v1/creative/generations/job1/semantic-qa",
        headers=auth(),
        json=payload,
    )

    assert response.status == 200
    body = await response.json()
    assert body["status"] == "needs_review"
    assert body["issues"] == ["Запрошенное изменение визуально не читается."]
    assert upstream.calls["semantic_qa"] == 1


@pytest.mark.asyncio
async def test_semantic_qa_rejects_invalid_scope_before_upstream(gateway, upstream):
    response = await gateway.post(
        "/v1/creative/generations/job1/semantic-qa",
        headers=auth(),
        json={"scope_id": "bad scope", "contract": {}},
    )

    assert response.status == 400
    assert upstream.calls["semantic_qa"] == 0


@pytest.mark.asyncio
async def test_capabilities_are_authenticated_and_do_not_touch_provider(gateway, upstream):
    denied = await gateway.get("/v1/capabilities")
    assert denied.status == 401
    response = await gateway.get("/v1/capabilities", headers=auth())
    assert response.status == 200
    assert await response.json() == {
        "contract_version": "1.0",
        "capabilities": ["generation", "semantic_qa", "render_pack", "usage"],
        "render_formats": ["square", "feed", "story", "landscape"],
    }
    assert upstream.calls == {"post": 0, "content": 0, "semantic_qa": 0}


@pytest.mark.asyncio
async def test_quota_and_generation_idempotency_are_before_provider_egress(gateway, upstream):
    payload = {
        "kind": "image",
        "prompt": "x",
        "scope_id": "tenant-a",
        "idempotency_key": "tenant-a:generate:1",
    }
    first = await gateway.post("/v1/creative/generations", headers=auth(), json=payload)
    assert first.status == 200
    replay = await gateway.post("/v1/creative/generations", headers=auth(), json=payload)
    assert replay.status == 200
    blocked = await gateway.post(
        "/v1/creative/generations",
        headers=auth(),
        json={**payload, "idempotency_key": "tenant-a:generate:2"},
    )
    assert blocked.status == 429
    assert upstream.calls["post"] == 2


@pytest.mark.asyncio
async def test_render_pack_exact_assets_digest_scope_and_idempotency(gateway, upstream):
    payload = {
        "source_job_id": "job1",
        "scope_id": "tenant-a",
        "idempotency_key": "tenant-a:render:one",
        "formats": ["square", "feed", "story", "landscape"],
        "composition": {
            "headline": "Заголовок",
            "body": "Точный текст",
            "cta": "Записаться",
            "layout": "lower_card",
            "typography": {"preset": "premium"},
            "brand": {
                "primary_color": "#172033",
                "accent_color": "#E9C46A",
                "text_color": "#FFFFFF",
            },
        },
    }
    first = await gateway.post("/v1/creative/render-packs", headers=auth(), json=payload)
    assert first.status == 200
    pack = await first.json()
    assert pack["status"] == "succeeded"
    assert {item["format_id"] for item in pack["assets"]} == set(FORMATS)
    pack_id = pack["id"]
    for asset in pack["assets"]:
        format_id = asset["format_id"]
        assert (asset["width"], asset["height"]) == FORMATS[format_id]
        assert asset["kind"] == "image"
        assert asset["mime_type"] == "image/jpeg"
        assert asset["asset_ready"] is True
        response = await gateway.get(
            f"/v1/creative/render-packs/{pack_id}/content/{format_id}?scope_id=tenant-a",
            headers=auth(),
        )
        raw = await response.read()
        assert response.status == 200
        assert hashlib.sha256(raw).hexdigest() == asset["sha256"]
        with Image.open(io.BytesIO(raw)) as image:
            assert image.size == FORMATS[format_id]

    replay = await gateway.post("/v1/creative/render-packs", headers=auth(), json=payload)
    assert replay.status == 200
    assert (await replay.json())["id"] == pack_id
    same_request_new_key = await gateway.post(
        "/v1/creative/render-packs",
        headers=auth(),
        json={**payload, "idempotency_key": "tenant-a:render:two"},
    )
    assert same_request_new_key.status == 200
    assert (await same_request_new_key.json())["id"] == pack_id
    assert upstream.calls["content"] == 1

    conflict = await gateway.post(
        "/v1/creative/render-packs",
        headers=auth(),
        json={**payload, "formats": ["feed"], "idempotency_key": "tenant-a:render:one"},
    )
    assert conflict.status == 409
    cross_scope = await gateway.get(
        f"/v1/creative/render-packs/{pack_id}?scope_id=tenant-b", headers=auth()
    )
    assert cross_scope.status == 404


def test_visual_gateway_typography_presets_match_clientplatform_contract():
    assert tuple(FONT_PRESETS) == tuple(VISUAL_TYPOGRAPHY_PRESETS)


def test_typography_preset_contract_and_auto_resolution_are_deterministic():
    base = {
        "source_job_id": "job1",
        "scope_id": "tenant-a",
        "idempotency_key": "tenant-a:font:test",
        "formats": ["square"],
    }
    for preset in FONT_PRESETS:
        _render_request(
            {
                **base,
                "composition": {
                    "headline": "Заголовок",
                    "body": "Текст",
                    "cta": "Записаться",
                    "typography": {"preset": preset},
                },
            }
        )

    assert _font_preset(
        {
            "headline": "Короткий оффер",
            "body": "Короткий текст",
            "cta": "Купить",
            "typography": {"preset": "auto"},
        },
        resolve_auto=True,
    ) == "bold_ad"
    assert _font_preset(
        {
            "headline": "Подробный материал",
            "body": "длинный текст " * 30,
            "cta": "",
            "typography": {"preset": "auto"},
        },
        resolve_auto=True,
    ) == "editorial"
    assert _font_preset(
        {
            "headline": "Очень длинный заголовок " * 5,
            "body": "Обычный текст",
            "cta": "",
            "typography": {"preset": "auto"},
        },
        resolve_auto=True,
    ) == "strict"
    assert _font_preset(
        {
            "headline": "Обычный заголовок средней длины",
            "body": "Обычный текст средней длины",
            "cta": "",
            "typography": {"preset": "auto"},
        },
        resolve_auto=True,
    ) == "modern"

    with pytest.raises(GatewayError, match="invalid_render_typography"):
        _render_request(
            {
                **base,
                "composition": {
                    "headline": "x",
                    "typography": {"preset": "comic"},
                },
            }
        )
    with pytest.raises(GatewayError, match="invalid_render_typography"):
        _render_request(
            {
                **base,
                "composition": {
                    "headline": "x",
                    "typography": {"preset": "modern", "family": "override"},
                },
            }
        )


@pytest.mark.asyncio
async def test_restart_safe_state_reuses_durable_pack(upstream, tmp_path, aiohttp_client):
    state = tmp_path / "state"
    config = GatewayConfig(
        token=TOKEN,
        upstream_url=str(upstream.make_url("")).rstrip("/"),
        upstream_token=UPSTREAM_TOKEN,
        state_dir=state,
    )
    payload = {
        "source_job_id": "job1",
        "scope_id": "tenant-a",
        "idempotency_key": "tenant-a:render:restart",
        "formats": ["feed"],
        "composition": {},
    }
    first_client = await aiohttp_client(create_app(config))
    first = await first_client.post("/v1/creative/render-packs", headers=auth(), json=payload)
    first_pack = await first.json()
    await first_client.close()

    second_client = await aiohttp_client(create_app(config))
    replay = await second_client.post("/v1/creative/render-packs", headers=auth(), json=payload)
    second_pack = await replay.json()
    assert second_pack["id"] == first_pack["id"]
    assert second_pack["status"] == "succeeded"
    assert upstream.calls["content"] == 1


@pytest.mark.asyncio
async def test_duplicate_submit_race_renders_once(gateway, upstream):
    payload = {
        "source_job_id": "job1",
        "scope_id": "tenant-race",
        "idempotency_key": "tenant-race:render:1",
        "formats": ["feed"],
        "composition": {"headline": "x"},
    }
    first, second = await asyncio.gather(
        gateway.post("/v1/creative/render-packs", headers=auth(), json=payload),
        gateway.post("/v1/creative/render-packs", headers=auth(), json=payload),
    )
    bodies = [await first.json(), await second.json()]
    assert len({item["id"] for item in bodies}) == 1
    assert upstream.calls["content"] == 1


@pytest.mark.asyncio
async def test_kind_mime_failure_is_normalized_and_never_ready(aiohttp_client, tmp_path):
    async def job(request):
        return web.json_response(
            {
                "id": "job1",
                "scope_id": request.query["scope_id"],
                "kind": "image",
                "status": "succeeded",
                "asset_ready": True,
            }
        )

    async def bad_content(_):
        return web.Response(body=b"not-video", content_type="video/mp4")

    upstream_app = web.Application()
    upstream_app.router.add_get("/v1/creative/generations/{job_id}", job)
    upstream_app.router.add_get("/v1/creative/generations/{job_id}/content", bad_content)
    upstream = await aiohttp_client(upstream_app)
    client = await aiohttp_client(
        create_app(
            GatewayConfig(
                token=TOKEN,
                upstream_url=str(upstream.make_url("")).rstrip("/"),
                upstream_token="",
                state_dir=tmp_path / "state",
            )
        )
    )
    response = await client.post(
        "/v1/creative/render-packs",
        headers=auth(),
        json={
            "source_job_id": "job1",
            "scope_id": "tenant-a",
            "idempotency_key": "tenant-a:render:bad",
            "formats": ["feed"],
            "composition": {},
        },
    )
    pack = await response.json()
    assert response.status == 200
    assert pack["status"] == "failed"
    assert pack["error_code"] == "provider_gateway_kind_mime_mismatch"
    assert pack["assets"] == []
