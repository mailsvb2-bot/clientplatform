from __future__ import annotations

import json
import urllib.error

from visual_provider_gateway.models import ProviderConfig
from visual_provider_gateway import yandex_model_catalog as catalog


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int = -1) -> bytes:
        return self.payload


def _config() -> ProviderConfig:
    return ProviderConfig(
        name="yandexart",
        base_url="https://ai.api.cloud.yandex.net",
        api_key="api-key",
        folder_id="folder",
        model_image="art://folder/aliceai-image-art-3.0",
        timeout_seconds=5,
    )


def test_catalog_is_disabled_without_credentials():
    snapshot = catalog.get_yandex_model_catalog(
        ProviderConfig(name="yandexart")
    )

    assert snapshot.configured is False
    assert snapshot.available is False


def test_catalog_reads_available_art_models_and_current_presence(monkeypatch):
    catalog.clear_yandex_model_catalog_cache()
    observed = {}
    payload = {
        "object": "list",
        "data": [
            {"id": "gpt://folder/aliceai-llm/latest"},
            {"id": "art://folder/aliceai-image-art-3.0"},
            {"id": "art://folder/aliceai-image-art-4.0@rc1"},
        ],
    }

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["authorization"] = request.headers.get("Authorization")
        observed["project"] = request.headers.get("Openai-project")
        observed["timeout"] = timeout
        return _Response(json.dumps(payload).encode("utf-8"))

    monkeypatch.setenv("YANDEX_API_KEY", "api-key")
    monkeypatch.setattr(catalog.urllib.request, "urlopen", fake_urlopen)

    snapshot = catalog.refresh_yandex_model_catalog(_config())

    assert snapshot.available is True
    assert snapshot.current_model_present is True
    assert snapshot.all_model_count == 3
    assert snapshot.art_models == (
        "art://folder/aliceai-image-art-3.0",
        "art://folder/aliceai-image-art-4.0@rc1",
    )
    assert observed["url"].endswith("/v1/models")
    assert observed["authorization"] == "Api-Key api-key"
    assert observed["project"] == "folder"


def test_catalog_prefers_dedicated_api_key_over_generation_key(monkeypatch):
    catalog.clear_yandex_model_catalog_cache()
    observed = {}

    def fake_urlopen(request, timeout):
        observed["authorization"] = request.headers.get("Authorization")
        return _Response(
            b'{"data":[{"id":"art://folder/aliceai-image-art-3.0"}]}'
        )

    monkeypatch.setenv("YANDEX_API_KEY", "generation-key")
    monkeypatch.setenv("YANDEX_MODEL_CATALOG_API_KEY", "catalog-key")
    monkeypatch.delenv("YANDEX_MODEL_CATALOG_AUTH_SCHEME", raising=False)
    monkeypatch.delenv("YANDEX_ART_AUTH_SCHEME", raising=False)
    monkeypatch.setattr(catalog.urllib.request, "urlopen", fake_urlopen)

    snapshot = catalog.refresh_yandex_model_catalog(_config())

    assert snapshot.available is True
    assert observed["authorization"] == "Api-Key catalog-key"


def test_catalog_allows_dedicated_key_even_when_generation_key_is_absent(monkeypatch):
    catalog.clear_yandex_model_catalog_cache()
    config = ProviderConfig(
        name="yandexart",
        base_url="https://ai.api.cloud.yandex.net",
        api_key="",
        folder_id="folder",
        model_image="art://folder/aliceai-image-art-3.0",
        timeout_seconds=5,
    )
    monkeypatch.setenv("YANDEX_MODEL_CATALOG_API_KEY", "catalog-key")
    monkeypatch.setattr(
        catalog.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            b'{"data":[{"id":"art://folder/aliceai-image-art-3.0"}]}'
        ),
    )

    snapshot = catalog.refresh_yandex_model_catalog(config)

    assert snapshot.configured is True
    assert snapshot.available is True


def test_dedicated_catalog_key_does_not_inherit_generation_bearer_scheme(monkeypatch):
    catalog.clear_yandex_model_catalog_cache()
    observed = {}

    def fake_urlopen(request, timeout):
        observed["authorization"] = request.headers.get("Authorization")
        return _Response(
            b'{"data":[{"id":"art://folder/aliceai-image-art-3.0"}]}'
        )

    monkeypatch.setenv("YANDEX_MODEL_CATALOG_API_KEY", "catalog-key")
    monkeypatch.delenv("YANDEX_MODEL_CATALOG_AUTH_SCHEME", raising=False)
    monkeypatch.setenv("YANDEX_ART_AUTH_SCHEME", "Bearer")
    monkeypatch.setenv("YANDEX_ART_IAM_TOKEN", "generation-iam-token")
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.setattr(catalog.urllib.request, "urlopen", fake_urlopen)

    snapshot = catalog.refresh_yandex_model_catalog(_config())

    assert snapshot.available is True
    assert observed["authorization"] == "Api-Key catalog-key"


def test_catalog_auth_scheme_can_be_separate_from_generation_auth(monkeypatch):
    catalog.clear_yandex_model_catalog_cache()
    observed = {}

    def fake_urlopen(request, timeout):
        observed["authorization"] = request.headers.get("Authorization")
        return _Response(
            b'{"data":[{"id":"art://folder/aliceai-image-art-3.0"}]}'
        )

    monkeypatch.setenv("YANDEX_MODEL_CATALOG_API_KEY", "catalog-token")
    monkeypatch.setenv("YANDEX_MODEL_CATALOG_AUTH_SCHEME", "Bearer")
    monkeypatch.setenv("YANDEX_ART_AUTH_SCHEME", "Api-Key")
    monkeypatch.setattr(catalog.urllib.request, "urlopen", fake_urlopen)

    catalog.refresh_yandex_model_catalog(_config())

    assert observed["authorization"] == "Bearer catalog-token"


def test_catalog_cache_avoids_repeated_provider_calls(monkeypatch):
    catalog.clear_yandex_model_catalog_cache()
    calls = 0

    def fake_urlopen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response(
            b'{"data":[{"id":"art://folder/aliceai-image-art-3.0"}]}'
        )

    monkeypatch.setenv("YANDEX_API_KEY", "api-key")
    monkeypatch.setenv("YANDEX_MODEL_CATALOG_TTL_SECONDS", "900")
    monkeypatch.setattr(catalog.urllib.request, "urlopen", fake_urlopen)

    first = catalog.refresh_yandex_model_catalog(_config())
    second = catalog.get_yandex_model_catalog(_config())

    assert first == second
    assert calls == 1


def test_health_path_returns_immediately_and_schedules_background_refresh(monkeypatch):
    catalog.clear_yandex_model_catalog_cache()
    scheduled = []

    monkeypatch.setattr(
        catalog,
        "_ensure_refresh",
        lambda key, config, ttl: scheduled.append((key, config.folder_id, ttl)),
    )

    snapshot = catalog.get_yandex_model_catalog(_config())

    assert snapshot.configured is True
    assert snapshot.available is False
    assert snapshot.error_code == ""
    assert len(scheduled) == 1
    assert scheduled[0][1] == "folder"


def test_catalog_reports_missing_current_model(monkeypatch):
    catalog.clear_yandex_model_catalog_cache()
    monkeypatch.setenv("YANDEX_API_KEY", "api-key")
    monkeypatch.setattr(
        catalog.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            b'{"data":[{"id":"art://folder/aliceai-image-art-4.0"}]}'
        ),
    )

    snapshot = catalog.refresh_yandex_model_catalog(_config())

    assert snapshot.available is True
    assert snapshot.current_model_present is False
    assert snapshot.art_models == ("art://folder/aliceai-image-art-4.0",)


def test_catalog_normalizes_http_and_invalid_response_failures(monkeypatch):
    catalog.clear_yandex_model_catalog_cache()
    monkeypatch.setenv("YANDEX_API_KEY", "api-key")

    def forbidden(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://ai.api.cloud.yandex.net/v1/models",
            403,
            "forbidden",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(catalog.urllib.request, "urlopen", forbidden)
    snapshot = catalog.refresh_yandex_model_catalog(_config())
    assert snapshot.error_code == "yandex_models_http_403"

    catalog.clear_yandex_model_catalog_cache()
    monkeypatch.setattr(
        catalog.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"[]"),
    )
    snapshot = catalog.refresh_yandex_model_catalog(_config())
    assert snapshot.error_code == "yandex_models_invalid_response"


def test_catalog_rejects_oversized_response(monkeypatch):
    catalog.clear_yandex_model_catalog_cache()
    monkeypatch.setenv("YANDEX_API_KEY", "api-key")
    monkeypatch.setenv("YANDEX_MODEL_CATALOG_MAX_BYTES", str(64 * 1024))
    monkeypatch.setattr(
        catalog.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"x" * (64 * 1024 + 1)),
    )

    snapshot = catalog.refresh_yandex_model_catalog(_config())

    assert snapshot.error_code == "yandex_models_response_too_large"