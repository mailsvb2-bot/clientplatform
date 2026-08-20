from __future__ import annotations

import json
from pathlib import Path

import pytest

from visual_provider_gateway.models import CreativeJob
from visual_provider_gateway.service import VisualGatewayService
from visual_provider_gateway.store import JobStore


def _api_surface():
    pytest.importorskip(
        "fastapi",
        reason="provider HTTP contract is exercised in the dedicated Visual Gateway Contract profile",
    )
    from fastapi.testclient import TestClient
    from visual_provider_gateway import app as app_module
    from visual_provider_gateway.app import GatewayPrincipal, app, require_auth

    return TestClient, app_module, GatewayPrincipal, app, require_auth


class FakeEngine:
    def __init__(self, asset: Path) -> None:
        self.asset = asset
        self.generations = 0
        self.polls = 0

    def generate(self, brief, *, wait_seconds=0):
        self.generations += 1
        assert brief.prompt
        assert wait_seconds >= 0
        return CreativeJob(provider="fake", kind=brief.kind, status="queued", external_id="provider-1", model="m1")

    def poll(self, job):
        self.polls += 1
        job.status = "succeeded"
        job.mime_type = "image/png"
        job.asset_path = str(self.asset)
        return job



class SequencedFailureEngine:
    def __init__(self, first_error: str) -> None:
        self.first_error = first_error
        self.generations = 0

    def generate(self, brief, *, wait_seconds=0):
        del wait_seconds
        self.generations += 1
        if self.generations == 1:
            return CreativeJob(
                provider="none",
                kind=brief.kind,
                status="failed",
                error_code=self.first_error,
            )
        return CreativeJob(
            provider="fake",
            kind=brief.kind,
            status="queued",
            external_id="provider-retry-1",
            model="m2",
        )

    def poll(self, job):
        return job

def payload(**updates):
    base = {
        "kind": "image",
        "prompt": "hello",
        "scope_id": "tenant-a",
        "idempotency_key": "request-0001",
    }
    base.update(updates)
    return base


def test_store_reservation_is_idempotent_and_client_scoped(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    first, created = store.reserve(client_id="client-a", scope_id="tenant-a", idempotency_key="request-0001", request_fingerprint="a" * 64, kind="image")
    assert created is True
    repeated, created = store.reserve(client_id="client-a", scope_id="tenant-a", idempotency_key="request-0001", request_fingerprint="a" * 64, kind="image")
    assert created is False
    assert repeated.id == first.id
    other, created = store.reserve(client_id="client-b", scope_id="tenant-a", idempotency_key="request-0001", request_fingerprint="a" * 64, kind="image")
    assert created is True
    assert other.id != first.id
    with pytest.raises(KeyError):
        store.get(first.id, client_id="client-b", scope_id="tenant-a")
    with pytest.raises(ValueError, match="payload_conflict"):
        store.reserve(client_id="client-a", scope_id="tenant-a", idempotency_key="request-0001", request_fingerprint="b" * 64, kind="image")


def test_service_submit_retry_does_not_duplicate_provider_call(tmp_path, monkeypatch):
    output = tmp_path / "out"
    output.mkdir()
    asset = output / "image-fake-provider-1.png"
    asset.write_bytes(b"png")
    monkeypatch.setenv("VISUAL_CREATIVE_OUTPUT_DIR", str(output))
    engine = FakeEngine(asset)
    svc = VisualGatewayService(store=JobStore(str(tmp_path / "jobs.sqlite3")), engine=engine)
    created = svc.submit(payload(), client_id="client-a")
    repeated = svc.submit(payload(), client_id="client-a")
    assert created["id"] == repeated["id"]
    assert engine.generations == 1
    done = svc.poll(created["id"], client_id="client-a", scope_id="tenant-a")
    assert done["status"] == "succeeded"
    path, mime = svc.content_path(created["id"], client_id="client-a", scope_id="tenant-a")
    assert path == asset.resolve()
    assert mime == "image/png"


def test_service_enforces_daily_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("VISUAL_GATEWAY_DAILY_JOB_LIMIT", "1")
    engine = FakeEngine(tmp_path / "unused")
    svc = VisualGatewayService(store=JobStore(str(tmp_path / "jobs.sqlite3")), engine=engine)
    svc.submit(payload(idempotency_key="request-0001"), client_id="client-a")
    with pytest.raises(PermissionError, match="daily_limit"):
        svc.submit(payload(idempotency_key="request-0002"), client_id="client-a")


def test_reference_urls_fail_closed_until_allowlisted(tmp_path, monkeypatch):
    engine = FakeEngine(tmp_path / "unused")
    svc = VisualGatewayService(store=JobStore(str(tmp_path / "jobs.sqlite3")), engine=engine)
    with pytest.raises(ValueError, match="reference_urls_disabled"):
        svc.submit(payload(reference_url="https://example.com/a.png"), client_id="client-a")

    monkeypatch.setenv("VISUAL_ALLOW_REFERENCE_URLS", "1")
    monkeypatch.setenv("VISUAL_REFERENCE_URL_ALLOWED_HOSTS", "assets.example.com")
    with pytest.raises(ValueError, match="host_not_allowed"):
        svc.submit(payload(idempotency_key="request-0002", reference_url="https://example.com/a.png"), client_id="client-a")
    result = svc.submit(payload(idempotency_key="request-0003", reference_url="https://assets.example.com/a.png"), client_id="client-a")
    assert result["status"] == "queued"


def test_api_multi_client_tokens_isolate_jobs(tmp_path, monkeypatch):
    TestClient, app_module, _GatewayPrincipal, app, _require_auth = _api_surface()
    monkeypatch.setenv("VISUAL_GATEWAY_CLIENT_TOKENS_JSON", json.dumps({"businesaios": "a" * 24, "clientplatform": "b" * 24}))
    output = tmp_path / "out"
    output.mkdir()
    asset = output / "asset.png"
    asset.write_bytes(b"png")
    svc = VisualGatewayService(store=JobStore(str(tmp_path / "jobs.sqlite3")), engine=FakeEngine(asset))
    monkeypatch.setattr(app_module, "service", lambda: svc)
    try:
        client = TestClient(app)
        created = client.post(
            "/v1/creative/generations",
            headers={"Authorization": "Bearer " + "a" * 24},
            json=payload(scope_id="tenant-1", idempotency_key="request-api-1"),
        )
        assert created.status_code == 200
        job_id = created.json()["id"]
        assert client.get(
            f"/v1/creative/generations/{job_id}?scope_id=tenant-1",
            headers={"Authorization": "Bearer " + "b" * 24},
        ).status_code == 404
        assert client.get(
            f"/v1/creative/generations/{job_id}?scope_id=tenant-1",
            headers={"Authorization": "Bearer " + "a" * 24},
        ).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_api_requires_auth(monkeypatch):
    TestClient, _app_module, _GatewayPrincipal, app, _require_auth = _api_surface()
    monkeypatch.setenv("VISUAL_GATEWAY_TOKEN", "secret")
    monkeypatch.delenv("VISUAL_GATEWAY_CLIENT_TOKENS_JSON", raising=False)
    monkeypatch.delenv("VISUAL_GATEWAY_ALLOW_ANONYMOUS", raising=False)
    client = TestClient(app)
    assert client.get("/v1/providers").status_code == 401


def test_api_health_is_unauthenticated():
    TestClient, _app_module, _GatewayPrincipal, app, _require_auth = _api_surface()
    client = TestClient(app)
    assert client.get("/healthz").json() == {"ok": True}


def test_api_validation_rejects_extra_fields():
    TestClient, _app_module, GatewayPrincipal, app, require_auth = _api_surface()
    app.dependency_overrides[require_auth] = lambda: GatewayPrincipal(client_id="test")
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/creative/generations",
            json={"kind": "image", "prompt": "x", "scope_id": "s", "idempotency_key": "request-0001", "unexpected": True},
        )
        assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_api_scope_isolates_jobs_within_same_client(tmp_path, monkeypatch):
    TestClient, app_module, _GatewayPrincipal, app, _require_auth = _api_surface()
    monkeypatch.setenv("VISUAL_GATEWAY_CLIENT_TOKENS_JSON", json.dumps({"clientplatform": "c" * 24}))
    svc = VisualGatewayService(store=JobStore(str(tmp_path / "jobs.sqlite3")), engine=FakeEngine(tmp_path / "asset.png"))
    monkeypatch.setattr(app_module, "service", lambda: svc)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + "c" * 24}
    created = client.post(
        "/v1/creative/generations",
        headers=headers,
        json=payload(scope_id="business-a", idempotency_key="request-scope-1"),
    )
    assert created.status_code == 200
    job_id = created.json()["id"]
    assert client.get(f"/v1/creative/generations/{job_id}?scope_id=business-b", headers=headers).status_code == 404
    assert client.get(f"/v1/creative/generations/{job_id}?scope_id=business-a", headers=headers).status_code == 200


def test_service_enforces_video_specific_daily_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("VISUAL_GATEWAY_DAILY_JOB_LIMIT", "100")
    monkeypatch.setenv("VISUAL_GATEWAY_DAILY_VIDEO_LIMIT", "1")
    engine = FakeEngine(tmp_path / "unused")
    svc = VisualGatewayService(store=JobStore(str(tmp_path / "jobs.sqlite3")), engine=engine)
    svc.submit(payload(kind="video", idempotency_key="request-video-1"), client_id="client-a")
    with pytest.raises(PermissionError, match="daily_video_limit"):
        svc.submit(payload(kind="video", idempotency_key="request-video-2"), client_id="client-a")


def test_service_deployment_country_is_authoritative_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("VISUAL_DEPLOYMENT_COUNTRY", "RU")
    monkeypatch.delenv("VISUAL_ALLOW_REQUEST_COUNTRY_OVERRIDE", raising=False)
    observed = {}

    class CountryEngine(FakeEngine):
        def generate(self, brief, *, wait_seconds=0):
            observed["country"] = brief.country_code
            return super().generate(brief, wait_seconds=wait_seconds)

    svc = VisualGatewayService(
        store=JobStore(str(tmp_path / "jobs.sqlite3")),
        engine=CountryEngine(tmp_path / "unused"),
    )
    svc.submit(payload(country_code="DE", idempotency_key="request-country-1"), client_id="client-a")
    assert observed["country"] == "RU"


def test_service_request_country_override_requires_operator_opt_in_and_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("VISUAL_DEPLOYMENT_COUNTRY", "RU")
    monkeypatch.setenv("VISUAL_ALLOW_REQUEST_COUNTRY_OVERRIDE", "1")
    monkeypatch.setenv("VISUAL_REQUEST_COUNTRY_ALLOWLIST", "NL,DE")
    observed = {}

    class CountryEngine(FakeEngine):
        def generate(self, brief, *, wait_seconds=0):
            observed["country"] = brief.country_code
            return super().generate(brief, wait_seconds=wait_seconds)

    svc = VisualGatewayService(
        store=JobStore(str(tmp_path / "jobs.sqlite3")),
        engine=CountryEngine(tmp_path / "unused"),
    )
    svc.submit(payload(country_code="DE", idempotency_key="request-country-2"), client_id="client-a")
    assert observed["country"] == "DE"
    with pytest.raises(ValueError, match="country_not_allowed"):
        svc.submit(payload(country_code="US", idempotency_key="request-country-3"), client_id="client-a")


def test_provider_snapshot_uses_effective_deployment_country(tmp_path, monkeypatch):
    monkeypatch.setenv("VISUAL_DEPLOYMENT_COUNTRY", "RU")
    monkeypatch.delenv("VISUAL_ALLOW_REQUEST_COUNTRY_OVERRIDE", raising=False)
    svc = VisualGatewayService(store=JobStore(str(tmp_path / "jobs.sqlite3")), engine=FakeEngine(tmp_path / "unused"))
    assert svc.snapshot("DE")["country_code"] == "RU"


def test_usage_endpoint_is_authenticated_and_client_scoped(monkeypatch, tmp_path):
    TestClient, app_module, _GatewayPrincipal, _app, _require_auth = _api_surface()

    monkeypatch.setenv(
        "VISUAL_GATEWAY_CLIENT_TOKENS_JSON",
        '{"clientplatform":"clientplatform-secret-123","other":"other-secret-123456"}',
    )
    monkeypatch.setenv("VISUAL_GATEWAY_DAILY_JOB_LIMIT", "30")
    monkeypatch.setenv("VISUAL_GATEWAY_DAILY_IMAGE_LIMIT", "30")
    monkeypatch.setenv("VISUAL_GATEWAY_DAILY_VIDEO_LIMIT", "5")
    monkeypatch.setenv("VISUAL_GATEWAY_MAX_ACTIVE_JOBS_PER_CLIENT", "3")
    monkeypatch.setenv(
        "VISUAL_GATEWAY_CLIENT_DAILY_LIMITS_JSON",
        '{"clientplatform":30,"other":9}',
    )
    monkeypatch.setenv(
        "VISUAL_GATEWAY_CLIENT_DAILY_IMAGE_LIMITS_JSON",
        '{"clientplatform":30,"other":8}',
    )

    store = JobStore(str(tmp_path / "usage.sqlite3"))
    fingerprint = "a" * 64
    store.reserve(
        client_id="clientplatform",
        scope_id="business-1",
        idempotency_key="request-0001",
        request_fingerprint=fingerprint,
        kind="image",
    )
    store.reserve(
        client_id="other",
        scope_id="tenant-2",
        idempotency_key="request-0002",
        request_fingerprint="b" * 64,
        kind="image",
    )

    app_module.service.cache_clear()
    monkeypatch.setattr(
        app_module,
        "service",
        lambda: VisualGatewayService(store=store),
    )
    client = TestClient(app_module.app)

    unauthorized = client.get("/v1/usage")
    assert unauthorized.status_code == 401

    response = client.get(
        "/v1/usage",
        headers={"Authorization": "Bearer clientplatform-secret-123"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["client_id"] == "clientplatform"
    assert payload["usage_semantics"] == "gateway_reservations_not_provider_billing"
    assert payload["jobs"] == {"used": 1, "limit": 30, "remaining": 29}
    assert payload["image"] == {"used": 1, "limit": 30, "remaining": 29}
    assert payload["active"] == {"used": 1, "limit": 3, "remaining": 2}
    assert payload["resets_at"].endswith("Z")


def test_usage_snapshot_reports_quota_rejections_as_reservations(monkeypatch, tmp_path):
    monkeypatch.setenv("VISUAL_GATEWAY_DAILY_JOB_LIMIT", "1")
    monkeypatch.setenv("VISUAL_GATEWAY_DAILY_IMAGE_LIMIT", "1")
    store = JobStore(str(tmp_path / "quota.sqlite3"))
    service = VisualGatewayService(store=store)
    for index in range(2):
        job, _created = store.reserve(
            client_id="clientplatform",
            scope_id="business-1",
            idempotency_key=f"request-{index:04d}",
            request_fingerprint=f"{index + 1:064x}",
            kind="image",
        )
        if index == 1:
            store.update(
                job.id,
                client_id="clientplatform",
                scope_id="business-1",
                provider="none",
                kind="image",
                status="failed",
                error_code="visual_gateway_quota_rejected",
            )
    payload = service.usage_snapshot("clientplatform")
    assert payload["jobs"]["used"] == 2
    assert payload["jobs"]["remaining"] == 0
    assert payload["image"]["used"] == 2
    assert payload["usage_semantics"] == "gateway_reservations_not_provider_billing"


def test_safe_terminal_failure_can_be_explicitly_retried_with_same_idempotency_key(tmp_path):
    engine = SequencedFailureEngine("visual_provider_submit_http_400")
    svc = VisualGatewayService(
        store=JobStore(str(tmp_path / "jobs.sqlite3")),
        engine=engine,
    )
    first = svc.submit(payload(), client_id="client-a")
    second = svc.submit(payload(), client_id="client-a")
    assert first["id"] == second["id"]
    assert first["status"] == "failed"
    assert second["status"] == "queued"
    assert second["error_code"] == ""
    assert engine.generations == 2


def test_no_provider_failure_can_be_retried_after_configuration_changes(tmp_path):
    engine = SequencedFailureEngine("no_visual_provider_available")
    svc = VisualGatewayService(
        store=JobStore(str(tmp_path / "jobs.sqlite3")),
        engine=engine,
    )
    assert svc.submit(payload(kind="video"), client_id="client-a")["status"] == "failed"
    assert svc.submit(payload(kind="video"), client_id="client-a")["status"] == "queued"
    assert engine.generations == 2


def test_ambiguous_provider_failure_never_retries_paid_submit(tmp_path):
    engine = SequencedFailureEngine("visual_provider_submit_timeout")
    svc = VisualGatewayService(
        store=JobStore(str(tmp_path / "jobs.sqlite3")),
        engine=engine,
    )
    first = svc.submit(payload(), client_id="client-a")
    second = svc.submit(payload(), client_id="client-a")
    assert first == second
    assert first["status"] == "failed"
    assert engine.generations == 1


def test_store_rearm_failed_is_atomic_and_error_allowlisted(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job, _ = store.reserve(
        client_id="client-a",
        scope_id="tenant-a",
        idempotency_key="request-rearm-1",
        request_fingerprint="c" * 64,
        kind="image",
    )
    store.update(
        job.id,
        client_id="client-a",
        scope_id="tenant-a",
        provider="none",
        kind="image",
        status="failed",
        error_code="visual_provider_submit_http_400",
    )
    allowed = frozenset({"visual_provider_submit_http_400"})
    assert store.rearm_failed(
        job.id, client_id="client-a", scope_id="tenant-a", allowed_error_codes=allowed
    ) is True
    assert store.rearm_failed(
        job.id, client_id="client-a", scope_id="tenant-a", allowed_error_codes=allowed
    ) is False
    refreshed = store.get(job.id, client_id="client-a", scope_id="tenant-a")
    assert refreshed.status == "running"
    assert refreshed.error_code == ""
