from __future__ import annotations

import base64
from pathlib import Path

import pytest

from visual_provider_gateway import providers
from visual_provider_gateway.engine import provider_order, provider_snapshot
from visual_provider_gateway.models import CreativeBrief, CreativeJob, ProviderConfig
from visual_provider_gateway.providers import SelfHostedVisualProvider
from services.yandex_iam_token import YandexIamTokenResult


def _clear_provider_routing(monkeypatch):
    for name in (
        "VISUAL_RU_IMAGE_ORDER",
        "VISUAL_RU_VIDEO_ORDER",
        "VISUAL_GLOBAL_IMAGE_ORDER",
        "VISUAL_GLOBAL_VIDEO_ORDER",
        "VISUAL_ALLOW_GLOBAL_PROVIDERS_IN_RU",
        "VISUAL_ALLOW_REQUEST_PROVIDER_OVERRIDE",
        "VISUAL_IMAGE_PROVIDER",
        "VISUAL_VIDEO_PROVIDER",
        "VISUAL_VIDEO_MOTION_PRIMARY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_transient_output_requirement_rejects_persistent_directory(tmp_path, monkeypatch):
    from visual_provider_gateway.models import ensure_output_dir

    persistent = tmp_path / "persistent-output"
    monkeypatch.setenv("VISUAL_TRANSIENT_OUTPUT_REQUIRED", "1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "ram-root"))

    with pytest.raises(RuntimeError, match="persistent_user_media_forbidden"):
        ensure_output_dir(str(persistent))


def test_ru_defaults_keep_global_clouds_out(monkeypatch):
    _clear_provider_routing(monkeypatch)
    assert provider_order("image", "RU") == ("yandexart", "gigachat", "selfhosted")
    assert provider_order("video", "RU") == ("selfhosted", "selfhosted_backup", "yandexart_motion")


def test_ru_video_prefers_native_providers_before_motion_fallback(monkeypatch):
    _clear_provider_routing(monkeypatch)
    monkeypatch.setenv("VISUAL_ALLOW_GLOBAL_PROVIDERS_IN_RU", "1")
    assert provider_order("video", "RU") == (
        "selfhosted",
        "selfhosted_backup",
        "runway",
        "openai",
        "yandexart_motion",
    )


def test_operator_can_explicitly_restore_motion_first(monkeypatch):
    _clear_provider_routing(monkeypatch)
    monkeypatch.setenv("VISUAL_VIDEO_MOTION_PRIMARY", "1")
    assert provider_order("video", "RU") == ("yandexart_motion", "selfhosted", "selfhosted_backup")


def test_global_defaults_prefer_runway_for_video(monkeypatch):
    _clear_provider_routing(monkeypatch)
    assert provider_order("video", "DE") == ("runway", "selfhosted", "openai")


def test_country_specific_order_overrides_defaults(monkeypatch):
    _clear_provider_routing(monkeypatch)
    monkeypatch.setenv("VISUAL_DE_IMAGE_ORDER", "runway,selfhosted")
    assert provider_order("image", "DE") == ("runway", "selfhosted")


def test_request_provider_override_is_disabled_by_default(monkeypatch):
    _clear_provider_routing(monkeypatch)
    with pytest.raises(ValueError, match="override_disabled"):
        provider_order("image", "RU", "openai")


def test_request_override_cannot_escape_country_policy(monkeypatch):
    _clear_provider_routing(monkeypatch)
    monkeypatch.setenv("VISUAL_ALLOW_REQUEST_PROVIDER_OVERRIDE", "1")
    with pytest.raises(ValueError, match="not_allowed_by_country_policy"):
        provider_order("image", "RU", "openai")
    assert provider_order("image", "RU", "yandexart") == ("yandexart",)


def test_ru_operator_can_explicitly_enable_global_provider(monkeypatch):
    _clear_provider_routing(monkeypatch)
    monkeypatch.setenv("VISUAL_ALLOW_GLOBAL_PROVIDERS_IN_RU", "1")
    monkeypatch.setenv("VISUAL_ALLOW_REQUEST_PROVIDER_OVERRIDE", "1")
    assert provider_order("image", "RU", "openai") == ("openai",)


def test_provider_snapshot_does_not_expose_credentials(monkeypatch):
    monkeypatch.setenv("VISUAL_OPENAI_API_KEY", "secret-openai")
    monkeypatch.setenv("RUNWAYML_API_SECRET", "secret-runway")
    monkeypatch.setenv("VISUAL_SELFHOST_TOKEN", "secret-selfhost")
    snapshot = provider_snapshot("DE")
    rendered = repr(snapshot)
    assert "secret-openai" not in rendered
    assert "secret-runway" not in rendered
    assert "secret-selfhost" not in rendered


def test_provider_snapshot_defaults_to_direct_images_and_versions_optional_orchestrator(monkeypatch):
    monkeypatch.setenv("YANDEX_ART_FOLDER_ID", "folder")
    monkeypatch.delenv("YANDEX_ART_PIPELINE", raising=False)
    monkeypatch.delenv("YANDEX_ART_ALLOW_DIRECT_FALLBACK", raising=False)
    monkeypatch.delenv("YANDEX_IMAGE_ORCHESTRATOR_MODEL", raising=False)

    snapshot = provider_snapshot("RU")
    yandex = snapshot["models"]["yandexart"]

    assert yandex["api_family"] == "openai_images"
    assert yandex["responses_required"] is False
    assert yandex["direct_fallback_allowed"] is False
    assert yandex["orchestrator_model"] == "gpt://folder/aliceai-llm/latest"

    monkeypatch.setenv("YANDEX_IMAGE_ORCHESTRATOR_MODEL", "gpt://folder/aliceai-llm")
    snapshot = provider_snapshot("RU")
    assert snapshot["models"]["yandexart"]["orchestrator_model"] == (
        "gpt://folder/aliceai-llm/latest"
    )

    monkeypatch.setenv("YANDEX_ART_PIPELINE", "responses")
    snapshot = provider_snapshot("RU")
    assert snapshot["models"]["yandexart"]["api_family"] == "responses_image_generation"
    assert snapshot["models"]["yandexart"]["responses_required"] is True

def test_gigachat_semantic_qa_is_non_generative_and_cleans_uploaded_file(
    tmp_path,
    monkeypatch,
):
    image_path = tmp_path / "result.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsemantic-image")
    transport_calls = []
    chat_payloads = []

    def fake_request(
        method,
        url,
        *,
        headers=None,
        body=None,
        timeout=30,
        max_bytes=0,
        ca_bundle_file="",
    ):
        transport_calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "body": body,
                "ca_bundle_file": ca_bundle_file,
            }
        )
        if url.endswith("/files"):
            assert b'name="purpose"' in body
            assert b"general" in body
            assert b'image/png' in body
            assert b"semantic-image" in body
            return 200, {"content-type": "application/json"}, b'{"id":"qa-file-1"}'
        if url.endswith("/files/qa-file-1/delete"):
            return 200, {"content-type": "application/json"}, b'{}'
        raise AssertionError(url)

    def fake_json_request(
        method,
        url,
        *,
        headers=None,
        payload=None,
        timeout=30,
        max_bytes=0,
        ca_bundle_file="",
    ):
        chat_payloads.append(payload)
        assert method == "POST"
        assert url.endswith("/chat/completions")
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"status":"needs_review",'
                            '"issues":["нет явной трансформации"],'
                            '"summary":"смысл передан не полностью"}'
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(providers, "_request", fake_request)
    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    provider = providers.GigaChatImageProvider(
        ProviderConfig(
            name="gigachat",
            base_url="https://api.giga.chat/v1",
            credentials="credentials",
            oauth_url="https://oauth.example.test",
            model_image="GigaChat-2-Pro",
            ca_bundle_file="/tmp/ca.pem",
        )
    )
    monkeypatch.setattr(provider, "_access_token", lambda: "token")

    result = provider.review_image_semantics(
        image_path=image_path,
        owner_request=(
            "ёж слушает ресурсное аудио и становится добрым и пушистым"
        ),
        semantic_flags=("listening", "transformation", "visible_state"),
    )

    assert result["status"] == "needs_review"
    assert result["issues"] == ["нет явной трансформации"]
    assert len(chat_payloads) == 1
    payload = chat_payloads[0]
    assert payload["function_call"] == "none"
    assert payload["messages"][0]["attachments"] == ["qa-file-1"]
    assert "Ничего не генерируй" in payload["messages"][0]["content"]
    assert sum(call["url"].endswith("/files") for call in transport_calls) == 1
    assert sum(
        call["url"].endswith("/files/qa-file-1/delete")
        for call in transport_calls
    ) == 1


def test_selfhosted_forwards_operator_selected_model(monkeypatch):
    observed = {}

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0):
        observed.update({"method": method, "url": url, "headers": headers, "payload": payload})
        return {"id": "worker-job", "status": "queued", "model": payload.get("model")}

    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    provider = SelfHostedVisualProvider(
        ProviderConfig(
            name="selfhosted",
            base_url="http://127.0.0.1:9000",
            api_key="worker-token",
            model_video="wan2.2-t2v-a14b",
        )
    )
    job = provider.submit(CreativeBrief(kind="video", prompt="cinematic rain", duration_seconds=5))
    assert observed["payload"]["model"] == "wan2.2-t2v-a14b"
    assert observed["headers"] == {"Authorization": "Bearer worker-token"}
    assert job.external_id == "worker-job"
    assert job.model == "wan2.2-t2v-a14b"


def test_selfhosted_backup_preserves_provider_identity(monkeypatch):
    observed = {}

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0):
        observed["url"] = url
        return {"id": "backup-job", "status": "queued", "model": payload.get("model")}

    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    provider = SelfHostedVisualProvider(
        ProviderConfig(
            name="selfhosted_backup",
            base_url="http://backup-worker:9000",
            api_key="backup-token",
            model_video="wan2.2-t2v-a14b",
        )
    )
    job = provider.submit(
        CreativeBrief(kind="video", prompt="cinematic rain", duration_seconds=5)
    )

    assert observed["url"].startswith("http://backup-worker:9000/")
    assert job.provider == "selfhosted_backup"
    assert job.external_id == "backup-job"


def test_openai_video_reference_fails_instead_of_being_ignored():
    from visual_provider_gateway.providers import OpenAIVisualProvider, ProviderTransportError

    provider = OpenAIVisualProvider(
        ProviderConfig(
            name="openai",
            base_url="https://api.openai.com/v1",
            api_key="test",
            model_video="sora-2",
        )
    )
    with pytest.raises(ProviderTransportError, match="reference_not_supported"):
        provider.submit(CreativeBrief(kind="video", prompt="animate this", reference_url="https://example.com/input.jpg"))


def test_yandexart_can_use_explicit_model_uri_without_separate_folder():
    from visual_provider_gateway.providers import YandexArtProvider

    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net:443",
            api_key="test",
            model_image="art://folder/aliceai-image-art-3.0",
        )
    )
    assert provider.configured("image") is True


def test_legacy_yandex_latest_uri_is_marked_deprecated():
    from visual_provider_gateway.engine import _model_lifecycle

    lifecycle = _model_lifecycle("art://folder/yandex-art/latest")

    assert lifecycle["status"] == "deprecated"
    assert lifecycle["deprecated_at"] == "2026-09-07"
    assert lifecycle["replacement"] == "aliceai-image-art-3.0"


def test_stale_deprecated_yandex_candidates_are_ignored_without_explicit_opt_in(monkeypatch):
    from visual_provider_gateway.providers import _yandex_image_model_candidates

    monkeypatch.setenv(
        "YANDEX_ART_MODEL_CANDIDATES",
        "art://folder/yandex-art/latest,art://folder/yandex-art-2.0",
    )
    monkeypatch.delenv("YANDEX_ALLOW_DEPRECATED_ART_MODELS", raising=False)
    config = ProviderConfig(
        name="yandexart",
        model_image="art://folder/aliceai-image-art-3.0",
        folder_id="folder",
    )

    assert _yandex_image_model_candidates(config) == (
        "art://folder/aliceai-image-art-3.0",
    )


def test_deprecated_yandex_candidate_requires_explicit_operator_opt_in(monkeypatch):
    from visual_provider_gateway.providers import _yandex_image_model_candidates

    monkeypatch.setenv(
        "YANDEX_ART_MODEL_CANDIDATES",
        "art://folder/yandex-art/latest",
    )
    monkeypatch.setenv("YANDEX_ALLOW_DEPRECATED_ART_MODELS", "1")
    config = ProviderConfig(
        name="yandexart",
        model_image="art://folder/aliceai-image-art-3.0",
        folder_id="folder",
    )

    assert _yandex_image_model_candidates(config) == (
        "art://folder/aliceai-image-art-3.0",
        "art://folder/yandex-art/latest",
    )


def test_yandexart_responses_image_generation_is_explicit_opt_in(monkeypatch, tmp_path):
    from visual_provider_gateway.providers import YandexArtProvider

    observed = {}
    encoded = base64.b64encode(b"responses-image").decode("ascii")

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0, ca_bundle_file=""):
        observed.update({"method": method, "url": url, "headers": headers, "payload": payload})
        return {
            "id": "response-123",
            "output": [
                {
                    "id": "image-call-123",
                    "type": "image_generation_call",
                    "status": "completed",
                    "result": encoded,
                    "file_id": "file-123",
                }
            ],
        }

    monkeypatch.setenv("YANDEX_ART_PIPELINE", "responses")
    monkeypatch.setenv("YANDEX_API_KEY", "test")
    monkeypatch.setenv("VISUAL_TRANSIENT_OUTPUT_REQUIRED", "1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(providers, "_json_request", fake_json_request)

    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net",
            api_key="test",
            folder_id="folder",
            model_image="art://folder/aliceai-image-art-3.0",
            model_orchestrator="gpt://folder/aliceai-llm/latest",
            output_dir=str(tmp_path / "visual"),
        )
    )
    job = provider.submit(
        CreativeBrief(
            kind="image",
            prompt="compressed-direct-prompt",
            aspect_ratio="16:9",
            metadata={
                "yandex_responses_input": (
                    "ёж, который слушает ресурсные аудио трансы и становится "
                    "добрым и пушистым"
                )
            },
        )
    )

    assert observed["method"] == "POST"
    assert observed["url"] == "https://ai.api.cloud.yandex.net/v1/responses"
    assert observed["headers"] == {
        "Authorization": "Api-Key test",
        "OpenAI-Project": "folder",
    }
    payload = observed["payload"]
    assert payload["model"] == "gpt://folder/aliceai-llm/latest"
    assert payload["store"] is False
    assert payload["input"].startswith("ёж, который слушает")
    assert payload["tool_choice"] == "required"
    assert payload["max_tool_calls"] == 1
    assert payload["parallel_tool_calls"] is False
    assert payload["tools"] == [
        {
            "type": "image_generation",
            "model": "aliceai-image-art-3.0",
            "quality": "high",
            "size": "1536x1024",
            "output_format": "png",
        }
    ]
    assert "превращение" in payload["instructions"]
    assert job.status == "succeeded"
    assert job.model == "art://folder/aliceai-image-art-3.0"
    assert job.provider_payload["transport"] == "responses_image_generation"
    assert job.provider_payload["orchestrator_model"] == "gpt://folder/aliceai-llm/latest"
    assert job.provider_payload["response_id"] == "response-123"
    assert job.provider_payload["file_id"] == "file-123"


def test_yandex_http_400_keeps_safe_validation_param_only():
    assert providers._safe_http_error_code(
        400,
        b'{"type":"invalid_request_error","param":"tool_choice","message":"details"}',
    ) == "http_400_param_tool_choice"
    assert providers._safe_http_error_code(
        400,
        b'{"param":"unsafe value with spaces","message":"secret-ish details"}',
    ) == "http_400"
    assert providers._safe_http_error_code(403, b'{"param":"tool_choice"}') == "http_403"


def test_yandex_responses_derives_folder_from_explicit_art_model_uri(monkeypatch, tmp_path):
    from visual_provider_gateway.providers import YandexArtProvider

    encoded = base64.b64encode(b"derived-folder-image").decode("ascii")
    observed = {}

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0, ca_bundle_file=""):
        observed.update({"url": url, "headers": headers, "payload": payload})
        return {
            "id": "response-derived-folder",
            "output": [{
                "id": "image-derived-folder",
                "type": "image_generation_call",
                "status": "completed",
                "result": encoded,
            }],
        }

    monkeypatch.setenv("YANDEX_ART_PIPELINE", "responses")
    monkeypatch.setenv("YANDEX_API_KEY", "test")
    monkeypatch.setenv("VISUAL_TRANSIENT_OUTPUT_REQUIRED", "1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(providers, "_json_request", fake_json_request)

    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net",
            api_key="test",
            folder_id="",
            model_image="art://folder-from-uri/aliceai-image-art-3.0",
            output_dir=str(tmp_path / "visual"),
        )
    )
    job = provider.submit(CreativeBrief(kind="image", prompt="hedgehog"))

    assert job.status == "succeeded"
    assert observed["headers"]["OpenAI-Project"] == "folder-from-uri"
    assert observed["payload"]["model"] == "gpt://folder-from-uri/aliceai-llm/latest"


def test_yandex_responses_403_fails_closed_by_default(monkeypatch):
    from visual_provider_gateway.providers import YandexArtProvider

    calls = []

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0, ca_bundle_file=""):
        calls.append((url, payload))
        raise providers.ProviderTransportError("http_403")

    monkeypatch.setenv("YANDEX_ART_PIPELINE", "responses")
    monkeypatch.delenv("YANDEX_ART_ALLOW_DIRECT_FALLBACK", raising=False)
    monkeypatch.setenv("YANDEX_API_KEY", "image-only-key")
    monkeypatch.setattr(providers, "_json_request", fake_json_request)

    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net",
            api_key="image-only-key",
            folder_id="folder",
            model_image="art://folder/aliceai-image-art-3.0",
        )
    )

    with pytest.raises(
        providers.ProviderTransportError,
        match="yandex_responses_not_authorized",
    ):
        provider.submit(CreativeBrief(kind="image", prompt="hedgehog"))

    assert [url for url, _ in calls] == [
        "https://ai.api.cloud.yandex.net/v1/responses",
    ]


def test_yandex_responses_403_can_use_direct_only_with_operator_opt_in(monkeypatch, tmp_path):
    from visual_provider_gateway.providers import YandexArtProvider

    encoded = base64.b64encode(b"direct-fallback-image").decode("ascii")
    calls = []

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0, ca_bundle_file=""):
        calls.append((url, payload))
        if url.endswith("/v1/responses"):
            raise providers.ProviderTransportError("http_403")
        assert url.endswith("/v1/images/generations")
        return {"data": [{"b64_json": encoded}]}

    monkeypatch.setenv("YANDEX_ART_PIPELINE", "responses")
    monkeypatch.setenv("YANDEX_ART_ALLOW_DIRECT_FALLBACK", "1")
    monkeypatch.setenv("YANDEX_API_KEY", "image-only-key")
    monkeypatch.setenv("VISUAL_TRANSIENT_OUTPUT_REQUIRED", "1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(providers, "_json_request", fake_json_request)

    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net",
            api_key="image-only-key",
            folder_id="folder",
            model_image="art://folder/aliceai-image-art-3.0",
            output_dir=str(tmp_path / "visual"),
        )
    )
    job = provider.submit(CreativeBrief(kind="image", prompt="hedgehog"))

    assert job.status == "succeeded"
    assert [url for url, _ in calls] == [
        "https://ai.api.cloud.yandex.net/v1/responses",
        "https://ai.api.cloud.yandex.net/v1/images/generations",
    ]
    assert job.provider_payload["transport"] == "openai_compat"

def test_yandex_responses_primary_403_retries_renewable_iam_without_direct_fallback(monkeypatch, tmp_path):
    from services.yandex_iam_token import YandexIamTokenResult
    from visual_provider_gateway.providers import YandexArtProvider

    encoded = base64.b64encode(b"renewable-responses-image").decode("ascii")
    calls = []

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0, ca_bundle_file=""):
        calls.append((url, dict(headers or {})))
        if headers["Authorization"] == "Api-Key image-only-key":
            raise providers.ProviderTransportError("http_403")
        assert headers["Authorization"] == "Bearer renewable-token"
        assert url.endswith("/v1/responses")
        return {
            "id": "response-renewable",
            "output": [{
                "id": "image-renewable",
                "type": "image_generation_call",
                "status": "completed",
                "result": encoded,
            }],
        }

    monkeypatch.setenv("YANDEX_ART_PIPELINE", "responses")
    monkeypatch.delenv("YANDEX_ART_ALLOW_DIRECT_FALLBACK", raising=False)
    monkeypatch.setenv("YANDEX_API_KEY", "image-only-key")
    monkeypatch.setenv("VISUAL_TRANSIENT_OUTPUT_REQUIRED", "1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    monkeypatch.setattr(
        providers,
        "get_yandex_art_iam_token",
        lambda: YandexIamTokenResult(
            configured=True,
            available=True,
            token="renewable-token",
            auth_mode="authorized_key",
        ),
    )

    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net",
            api_key="image-only-key",
            folder_id="folder",
            model_image="art://folder/aliceai-image-art-3.0",
            output_dir=str(tmp_path / "visual"),
        )
    )
    job = provider.submit(CreativeBrief(kind="image", prompt="hedgehog"))

    assert job.status == "succeeded"
    assert [headers["Authorization"] for _url, headers in calls] == [
        "Api-Key image-only-key",
        "Bearer renewable-token",
    ]
    assert all(url.endswith("/v1/responses") for url, _headers in calls)
    assert job.provider_payload["transport"] == "responses_image_generation"


def test_yandex_responses_ambiguous_error_never_falls_back_to_direct_images(monkeypatch):
    from visual_provider_gateway.providers import YandexArtProvider

    calls = []

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0, ca_bundle_file=""):
        calls.append(url)
        raise providers.ProviderTransportError("TimeoutError")

    monkeypatch.setenv("YANDEX_ART_PIPELINE", "responses")
    monkeypatch.setenv("YANDEX_API_KEY", "key")
    monkeypatch.setattr(providers, "_json_request", fake_json_request)

    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net",
            api_key="key",
            folder_id="folder",
            model_image="art://folder/aliceai-image-art-3.0",
        )
    )

    with pytest.raises(providers.ProviderTransportError, match="TimeoutError"):
        provider.submit(CreativeBrief(kind="image", prompt="hedgehog"))

    assert calls == ["https://ai.api.cloud.yandex.net/v1/responses"]


def test_alice_ai_art_defaults_to_openai_compatible_images_api(tmp_path, monkeypatch):
    monkeypatch.delenv("YANDEX_ART_PIPELINE", raising=False)
    from visual_provider_gateway.providers import YandexArtProvider

    calls = []
    encoded = base64.b64encode(b"alice-image").decode("ascii")

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0):
        calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
            }
        )
        return {"data": [{"b64_json": encoded}]}

    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    monkeypatch.setenv("YANDEX_API_KEY", "durable-api-key")
    monkeypatch.setenv("VISUAL_TRANSIENT_OUTPUT_REQUIRED", "1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net:443",
            api_key="durable-api-key",
            model_image="art://folder/aliceai-image-art-3.0",
            folder_id="folder",
            output_dir=str(tmp_path / "visual"),
        )
    )

    job = provider.submit(
        CreativeBrief(
            kind="image",
            prompt="clean product photo",
            aspect_ratio="16:9",
            seed=12,
        )
    )

    assert job.status == "succeeded"
    assert Path(job.asset_path).read_bytes() == b"alice-image"
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://ai.api.cloud.yandex.net:443/v1/images/generations"
    assert calls[0]["headers"] == {
        "Authorization": "Api-Key durable-api-key",
        "OpenAI-Project": "folder",
    }
    assert calls[0]["payload"] == {
        "model": "art://folder/aliceai-image-art-3.0",
        "prompt": "clean product photo",
        "size": "1536x1024",
    }
    assert job.provider_payload["transport"] == "openai_compat"


def test_alice_images_api_rejects_overlong_prompt_before_network(monkeypatch):
    from visual_provider_gateway.providers import YandexArtProvider

    monkeypatch.delenv("YANDEX_ART_PIPELINE", raising=False)
    monkeypatch.setenv("YANDEX_API_KEY", "durable-api-key")
    calls = []
    monkeypatch.setattr(
        providers,
        "_json_request",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net",
            api_key="durable-api-key",
            model_image="art://folder/aliceai-image-art-3.0",
            folder_id="folder",
        )
    )

    with pytest.raises(
        providers.ProviderTransportError,
        match="yandex_prompt_too_long",
    ):
        provider.submit(CreativeBrief(kind="image", prompt="x" * 501))

    assert calls == []


def test_yandexart_poll_materializes_native_operation_result(tmp_path, monkeypatch):
    from visual_provider_gateway.providers import YandexArtProvider

    encoded = base64.b64encode(b"jpeg-bytes").decode("ascii")
    observed = {}

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0):
        observed.update({"method": method, "url": url, "headers": headers})
        return {
            "id": "operation-123",
            "done": True,
            "response": {
                "image": encoded,
                "modelVersion": "2026-09-01",
            },
        }

    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    monkeypatch.setenv("YANDEX_API_KEY", "durable-api-key")
    monkeypatch.setenv("VISUAL_TRANSIENT_OUTPUT_REQUIRED", "1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net:443",
            api_key="durable-api-key",
            model_image="art://folder/aliceai-image-art-3.0",
            folder_id="folder",
            output_dir=str(tmp_path / "visual"),
        )
    )
    job = CreativeJob(
        provider="yandexart",
        kind="image",
        status="running",
        external_id="operation-123",
        model="art://folder/aliceai-image-art-3.0",
    )

    result = provider.poll(job)

    assert result.status == "succeeded"
    assert result.mime_type == "image/jpeg"
    assert Path(result.asset_path).read_bytes() == b"jpeg-bytes"
    assert result.provider_payload["model_version"] == "2026-09-01"
    assert observed["method"] == "GET"
    assert observed["url"] == (
        "https://operation.api.cloud.yandex.net:443/operations/operation-123"
    )
    assert observed["headers"] == {"Authorization": "Api-Key durable-api-key"}


def test_yandexart_motion_waits_for_native_image_operation(monkeypatch):
    from visual_provider_gateway.providers import YandexArtMotionVideoProvider

    provider = YandexArtMotionVideoProvider(
        ProviderConfig(
            name="yandexart_motion",
            base_url="https://ai.api.cloud.yandex.net:443",
            api_key="key",
            model_image="art://folder/aliceai-image-art-3.0",
            folder_id="folder",
        )
    )
    queued = CreativeJob(
        provider="yandexart",
        kind="image",
        status="running",
        external_id="operation-123",
        model="art://folder/aliceai-image-art-3.0",
    )
    monkeypatch.setattr(
        providers.YandexArtProvider,
        "submit",
        lambda _self, _brief: queued,
    )

    result = provider.submit(
        CreativeBrief(kind="video", prompt="animate", duration_seconds=5)
    )

    assert result.provider == "yandexart_motion"
    assert result.kind == "video"
    assert result.status == "running"
    assert result.external_id == "operation-123"
    assert result.provider_payload["motion_duration_seconds"] == 5


def test_yandex_motion_retries_local_render_without_resubmitting_paid_keyframe(
    tmp_path,
    monkeypatch,
):
    from visual_provider_gateway.providers import (
        ProviderTransportError,
        YandexArtMotionVideoProvider,
    )

    source = tmp_path / "paid-keyframe.png"
    source.write_bytes(b"keyframe")
    video = tmp_path / "recovered.mp4"
    paid_submits = []
    render_attempts = []

    def fake_paid_submit(_self, _brief):
        paid_submits.append(1)
        return CreativeJob(
            provider="yandexart",
            kind="image",
            status="succeeded",
            external_id="paid-keyframe-1",
            model="art://folder/aliceai-image-art-3.0",
            mime_type="image/png",
            asset_path=str(source),
        )

    def fake_render(_config, **kwargs):
        render_attempts.append(kwargs["image_path"])
        if len(render_attempts) == 1:
            raise ProviderTransportError("motion_render_failed")
        video.write_bytes(b"mp4")
        return str(video)

    monkeypatch.setattr(providers.YandexArtProvider, "submit", fake_paid_submit)
    monkeypatch.setattr(providers, "_render_motion_video", fake_render)
    provider = YandexArtMotionVideoProvider(
        ProviderConfig(
            name="yandexart_motion",
            base_url="https://ai.api.cloud.yandex.net:443",
            api_key="key",
            model_image="art://folder/aliceai-image-art-3.0",
            folder_id="folder",
            output_dir=str(tmp_path),
        )
    )

    first = provider.submit(
        CreativeBrief(kind="video", prompt="animate", duration_seconds=5)
    )

    assert first.status == "running"
    assert first.error_code == "motion_render_retryable"
    assert first.provider_payload["motion_source_path"] == str(source)
    assert source.exists()
    assert len(paid_submits) == 1

    recovered = provider.poll(first)

    assert recovered.status == "succeeded"
    assert recovered.mime_type == "video/mp4"
    assert recovered.asset_path == str(video)
    assert not source.exists()
    assert "motion_source_path" not in recovered.provider_payload
    assert len(paid_submits) == 1
    assert render_attempts == [str(source), str(source)]


def test_yandex_motion_bounds_local_render_retries_without_new_provider_submit(
    tmp_path,
    monkeypatch,
):
    from visual_provider_gateway.providers import (
        ProviderTransportError,
        YandexArtMotionVideoProvider,
    )

    source = tmp_path / "paid-keyframe.png"
    source.write_bytes(b"keyframe")
    paid_submits = []

    def fake_paid_submit(_self, _brief):
        paid_submits.append(1)
        return CreativeJob(
            provider="yandexart",
            kind="image",
            status="succeeded",
            external_id="paid-keyframe-1",
            model="art://folder/aliceai-image-art-3.0",
            mime_type="image/png",
            asset_path=str(source),
        )

    monkeypatch.setattr(providers.YandexArtProvider, "submit", fake_paid_submit)
    monkeypatch.setattr(
        providers,
        "_render_motion_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProviderTransportError("motion_render_failed")
        ),
    )
    provider = YandexArtMotionVideoProvider(
        ProviderConfig(
            name="yandexart_motion",
            base_url="https://ai.api.cloud.yandex.net:443",
            api_key="key",
            model_image="art://folder/aliceai-image-art-3.0",
            folder_id="folder",
            output_dir=str(tmp_path),
        )
    )

    first = provider.submit(
        CreativeBrief(kind="video", prompt="animate", duration_seconds=5)
    )
    second = provider.poll(first)
    terminal = provider.poll(second)

    assert first.status == "running"
    assert second.status == "running"
    assert terminal.status == "failed"
    assert terminal.error_code == "motion_render_failed"
    assert not source.exists()
    assert len(paid_submits) == 1


def test_current_alice_model_failure_is_not_masked_by_deprecated_fallback(monkeypatch):
    monkeypatch.setenv("YANDEX_ART_PIPELINE", "images")
    from visual_provider_gateway.providers import (
        ProviderTransportError,
        YandexArtProvider,
    )

    calls = []

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0):
        calls.append((url, payload))
        raise ProviderTransportError("http_400")

    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    monkeypatch.setenv("YANDEX_API_KEY", "key")
    monkeypatch.delenv("YANDEX_ART_MODEL_CANDIDATES", raising=False)
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net:443",
            api_key="key",
            model_image="art://folder/aliceai-image-art-3.0",
            folder_id="folder",
        )
    )

    with pytest.raises(ProviderTransportError, match="http_400"):
        provider.submit(CreativeBrief(kind="image", prompt="product"))

    assert len(calls) == 1
    assert calls[0][0].endswith("/v1/images/generations")
    assert calls[0][1]["model"] == "art://folder/aliceai-image-art-3.0"


def test_alice_ai_art_auth_rejection_can_use_renewable_iam(tmp_path, monkeypatch):
    monkeypatch.setenv("YANDEX_ART_PIPELINE", "images")
    from services.yandex_iam_token import YandexIamTokenResult
    from visual_provider_gateway.providers import (
        ProviderTransportError,
        YandexArtProvider,
    )

    calls = []
    encoded = base64.b64encode(b"renewable-image").decode("ascii")

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0):
        calls.append((url, headers))
        if headers["Authorization"] == "Api-Key durable-api-key":
            raise ProviderTransportError("http_403")
        return {"data": [{"b64_json": encoded}]}

    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    monkeypatch.setattr(
        providers,
        "get_yandex_art_iam_token",
        lambda: YandexIamTokenResult(
            configured=True,
            available=True,
            token="renewable-token",
            auth_mode="authorized_key",
        ),
    )
    monkeypatch.setenv("YANDEX_API_KEY", "durable-api-key")
    monkeypatch.setenv("VISUAL_TRANSIENT_OUTPUT_REQUIRED", "1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net:443",
            api_key="durable-api-key",
            model_image="art://folder/aliceai-image-art-3.0",
            folder_id="folder",
            output_dir=str(tmp_path / "visual"),
        )
    )

    job = provider.submit(CreativeBrief(kind="image", prompt="product"))

    assert job.status == "succeeded"
    assert Path(job.asset_path).read_bytes() == b"renewable-image"
    assert [headers["Authorization"] for _url, headers in calls] == [
        "Api-Key durable-api-key",
        "Bearer renewable-token",
    ]


def test_yandexart_does_not_retry_model_after_ambiguous_submit(monkeypatch):
    monkeypatch.setenv("YANDEX_ART_PIPELINE", "images")
    from visual_provider_gateway.providers import (
        ProviderTransportError,
        YandexArtProvider,
    )

    calls = []

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0):
        calls.append(payload["model"])
        raise ProviderTransportError("TimeoutError")

    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    monkeypatch.setenv("YANDEX_API_KEY", "key")
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net:443",
            api_key="key",
            model_image="art://folder/aliceai-image-art-3.0",
            folder_id="folder",
        )
    )

    with pytest.raises(ProviderTransportError, match="TimeoutError"):
        provider.submit(CreativeBrief(kind="image", prompt="product"))

    assert calls == ["art://folder/aliceai-image-art-3.0"]


def test_yandex_api_key_wins_over_stale_iam_token(monkeypatch):
    from visual_provider_gateway.engine import provider_configs
    from visual_provider_gateway.providers import YandexArtProvider

    monkeypatch.setenv("YANDEX_API_KEY", "durable-api-key")
    monkeypatch.setenv("YANDEX_ART_IAM_TOKEN", "expired-iam-token")
    monkeypatch.delenv("YANDEX_ART_AUTH_SCHEME", raising=False)
    monkeypatch.setenv("YANDEX_ART_FOLDER_ID", "folder")

    config = provider_configs()["yandexart"]
    provider = YandexArtProvider(config)

    assert config.api_key == "durable-api-key"
    assert provider._authorization() == "Api-Key durable-api-key"


def test_yandex_iam_token_still_works_when_no_api_key_exists(monkeypatch):
    from visual_provider_gateway.engine import provider_configs
    from visual_provider_gateway.providers import YandexArtProvider

    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.setenv("YANDEX_ART_IAM_TOKEN", "current-iam-token")
    monkeypatch.delenv("YANDEX_ART_AUTH_SCHEME", raising=False)
    monkeypatch.setenv("YANDEX_ART_FOLDER_ID", "folder")

    config = provider_configs()["yandexart"]
    provider = YandexArtProvider(config)

    assert config.api_key == "current-iam-token"
    assert provider._authorization() == "Bearer current-iam-token"


def test_provider_snapshot_reports_native_video_mode(monkeypatch):
    from visual_provider_gateway.engine import provider_snapshot

    _clear_provider_routing(monkeypatch)
    monkeypatch.setenv("VISUAL_CREATIVE_ENABLED", "1")
    monkeypatch.setenv("VISUAL_SELFHOST_BASE_URL", "http://worker:9000")
    monkeypatch.setenv("VISUAL_SELFHOST_VIDEO_MODEL", "wan2.2-t2v-a14b")
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_ART_IAM_TOKEN", raising=False)

    snapshot = provider_snapshot("RU")

    assert snapshot["video_generation_mode"] == "native"
    assert snapshot["configured_video_native"] == ("selfhosted",)
    assert snapshot["configured_video_motion"] == ()


def test_provider_snapshot_reports_motion_fallback_mode(monkeypatch):
    from visual_provider_gateway.engine import provider_snapshot

    _clear_provider_routing(monkeypatch)
    monkeypatch.setenv("VISUAL_CREATIVE_ENABLED", "1")
    monkeypatch.setenv("YANDEX_API_KEY", "key")
    monkeypatch.setenv("YANDEX_ART_FOLDER_ID", "folder")
    monkeypatch.delenv("VISUAL_SELFHOST_BASE_URL", raising=False)
    monkeypatch.delenv("VISUAL_SELFHOST_BACKUP_BASE_URL", raising=False)

    class Catalog:
        configured = True
        available = True
        current_model_present = True
        art_models = ("art://folder/aliceai-image-art-3.0",)
        all_model_count = 1
        error_code = ""

    monkeypatch.setattr(
        "visual_provider_gateway.engine.get_yandex_model_catalog",
        lambda _config: Catalog(),
    )
    snapshot = provider_snapshot("RU")

    assert snapshot["video_generation_mode"] == "motion"
    assert snapshot["configured_video_native"] == ()
    assert snapshot["configured_video_motion"] == ("yandexart_motion",)


def test_provider_snapshot_strips_base_url_credentials_and_paths(monkeypatch):
    monkeypatch.setenv("VISUAL_OPENAI_BASE_URL", "https://user:secret@example.com/private/api?token=x")
    snapshot = provider_snapshot("DE")
    value = snapshot["providers"]["openai"]["base_url"]
    assert value == "https://example.com"
    assert "secret" not in repr(snapshot)
    assert "private" not in repr(snapshot)


def test_submit_does_not_failover_after_ambiguous_provider_error_by_default(monkeypatch):
    from visual_provider_gateway.engine import VisualCreativeEngine
    from visual_provider_gateway.models import CreativeJob

    calls = []

    class BrokenProvider:
        def configured(self, kind):
            return True
        def submit(self, brief):
            calls.append("broken")
            raise providers.ProviderTransportError("TimeoutError")

    class SecondProvider:
        def configured(self, kind):
            return True
        def submit(self, brief):
            calls.append("second")
            return CreativeJob(provider="second", kind=brief.kind, status="queued", external_id="j2")

    monkeypatch.setattr("visual_provider_gateway.engine.provider_order", lambda *_args, **_kwargs: ("broken", "second"))
    monkeypatch.setattr("visual_provider_gateway.engine.build_provider", lambda name: BrokenProvider() if name == "broken" else SecondProvider())
    monkeypatch.delenv("VISUAL_ALLOW_PROVIDER_FAILOVER_AFTER_ERROR", raising=False)
    job = VisualCreativeEngine(enabled=True).submit(CreativeBrief(kind="image", prompt="x"))
    assert job.status == "failed"
    assert calls == ["broken"]


def test_submit_fails_over_after_proven_connection_refusal(monkeypatch):
    from visual_provider_gateway.engine import VisualCreativeEngine
    from visual_provider_gateway.models import CreativeJob

    calls = []

    class UnreachableProvider:
        def configured(self, kind):
            return True

        def submit(self, brief):
            calls.append("primary")
            raise providers.ProviderTransportError("connect_unreachable")

    class BackupProvider:
        def configured(self, kind):
            return True

        def submit(self, brief):
            calls.append("backup")
            return CreativeJob(
                provider="selfhosted_backup",
                kind=brief.kind,
                status="queued",
                external_id="backup-1",
            )

    monkeypatch.setattr(
        "visual_provider_gateway.engine.provider_order",
        lambda *_args, **_kwargs: ("selfhosted", "selfhosted_backup"),
    )
    monkeypatch.setattr(
        "visual_provider_gateway.engine.build_provider",
        lambda name: UnreachableProvider() if name == "selfhosted" else BackupProvider(),
    )

    job = VisualCreativeEngine(enabled=True).submit(
        CreativeBrief(kind="video", prompt="x")
    )

    assert job.provider == "selfhosted_backup"
    assert calls == ["primary", "backup"]


def test_submit_fails_over_after_definitive_invalid_request(monkeypatch):
    from visual_provider_gateway.engine import VisualCreativeEngine
    from visual_provider_gateway.models import CreativeJob

    calls = []

    class RejectedProvider:
        def configured(self, kind):
            return True

        def submit(self, brief):
            calls.append("rejected")
            raise providers.ProviderTransportError("http_422")

    class MotionFallback:
        def configured(self, kind):
            return True

        def submit(self, brief):
            calls.append("motion")
            return CreativeJob(
                provider="yandexart_motion",
                kind=brief.kind,
                status="succeeded",
                external_id="motion-1",
            )

    monkeypatch.setattr(
        "visual_provider_gateway.engine.provider_order",
        lambda *_args, **_kwargs: ("runway", "yandexart_motion"),
    )
    monkeypatch.setattr(
        "visual_provider_gateway.engine.build_provider",
        lambda name: RejectedProvider() if name == "runway" else MotionFallback(),
    )
    job = VisualCreativeEngine(enabled=True).submit(
        CreativeBrief(kind="video", prompt="x")
    )

    assert job.provider == "yandexart_motion"
    assert calls == ["rejected", "motion"]


def test_submit_fails_over_after_definitive_auth_rejection(monkeypatch):
    from visual_provider_gateway.engine import VisualCreativeEngine
    from visual_provider_gateway.models import CreativeJob

    calls = []

    class UnauthorizedProvider:
        def configured(self, kind):
            return True
        def submit(self, brief):
            calls.append("unauthorized")
            raise providers.ProviderTransportError("http_403")

    class SecondProvider:
        def configured(self, kind):
            return True
        def submit(self, brief):
            calls.append("second")
            return CreativeJob(
                provider="second",
                kind=brief.kind,
                status="queued",
                external_id="j2",
            )

    monkeypatch.setattr(
        "visual_provider_gateway.engine.provider_order",
        lambda *_args, **_kwargs: ("unauthorized", "second"),
    )
    monkeypatch.setattr(
        "visual_provider_gateway.engine.build_provider",
        lambda name: UnauthorizedProvider() if name == "unauthorized" else SecondProvider(),
    )
    monkeypatch.delenv("VISUAL_ALLOW_PROVIDER_FAILOVER_AFTER_ERROR", raising=False)

    job = VisualCreativeEngine(enabled=True).submit(
        CreativeBrief(kind="image", prompt="x")
    )

    assert job.provider == "second"
    assert calls == ["unauthorized", "second"]


def test_explicit_provider_does_not_escape_auth_rejection(monkeypatch):
    from visual_provider_gateway.engine import VisualCreativeEngine

    calls = []

    class UnauthorizedProvider:
        def configured(self, kind):
            return True
        def submit(self, brief):
            calls.append("unauthorized")
            raise providers.ProviderTransportError("http_401")

    monkeypatch.setattr(
        "visual_provider_gateway.engine.provider_order",
        lambda *_args, **_kwargs: ("unauthorized", "second"),
    )
    monkeypatch.setattr(
        "visual_provider_gateway.engine.build_provider",
        lambda _name: UnauthorizedProvider(),
    )
    monkeypatch.delenv("VISUAL_ALLOW_PROVIDER_FAILOVER_AFTER_ERROR", raising=False)

    job = VisualCreativeEngine(enabled=True).submit(
        CreativeBrief(
            kind="image",
            prompt="x",
            preferred_provider="unauthorized",
        )
    )

    assert job.status == "failed"
    assert job.error_code == "visual_provider_submit_http_401"
    assert calls == ["unauthorized"]


def test_submit_can_failover_only_with_explicit_operator_opt_in(monkeypatch):
    from visual_provider_gateway.engine import VisualCreativeEngine
    from visual_provider_gateway.models import CreativeJob

    calls = []

    class BrokenProvider:
        def configured(self, kind):
            return True
        def submit(self, brief):
            calls.append("broken")
            raise providers.ProviderTransportError("TimeoutError")

    class SecondProvider:
        def configured(self, kind):
            return True
        def submit(self, brief):
            calls.append("second")
            return CreativeJob(provider="second", kind=brief.kind, status="queued", external_id="j2")

    monkeypatch.setattr("visual_provider_gateway.engine.provider_order", lambda *_args, **_kwargs: ("broken", "second"))
    monkeypatch.setattr("visual_provider_gateway.engine.build_provider", lambda name: BrokenProvider() if name == "broken" else SecondProvider())
    monkeypatch.setenv("VISUAL_ALLOW_PROVIDER_FAILOVER_AFTER_ERROR", "1")
    job = VisualCreativeEngine(enabled=True).submit(CreativeBrief(kind="image", prompt="x"))
    assert job.provider == "second"
    assert calls == ["broken", "second"]


def test_poll_transient_transport_error_keeps_job_retryable(monkeypatch):
    from visual_provider_gateway.engine import VisualCreativeEngine
    from visual_provider_gateway.models import CreativeJob

    class PollProvider:
        def poll(self, job):
            raise providers.ProviderTransportError("TimeoutError")

    monkeypatch.setattr("visual_provider_gateway.engine.build_provider", lambda _name: PollProvider())
    job = CreativeJob(provider="x", kind="video", status="running", external_id="job1")
    result = VisualCreativeEngine(enabled=True).poll(job)
    assert result.status == "running"
    assert result.error_code == "visual_provider_poll_transient"


def test_poll_terminal_http_error_fails_job(monkeypatch):
    from visual_provider_gateway.engine import VisualCreativeEngine
    from visual_provider_gateway.models import CreativeJob

    class PollProvider:
        def poll(self, job):
            raise providers.ProviderTransportError("http_404")

    monkeypatch.setattr("visual_provider_gateway.engine.build_provider", lambda _name: PollProvider())
    job = CreativeJob(provider="x", kind="video", status="running", external_id="job1")
    result = VisualCreativeEngine(enabled=True).poll(job)
    assert result.status == "failed"
    assert result.error_code == "visual_provider_poll_http_404"


def test_provider_defaults_use_current_yandex_and_gigachat_endpoints(monkeypatch):
    from visual_provider_gateway.engine import provider_configs

    for name in (
        "YANDEX_ART_BASE_URL",
        "VISUAL_GIGACHAT_BASE_URL",
        "GIGACHAT_BASE_URL",
        "VISUAL_GIGACHAT_OAUTH_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    configs = provider_configs()
    assert configs["yandexart"].base_url == "https://ai.api.cloud.yandex.net:443"
    assert configs["yandexart_motion"].base_url == "https://ai.api.cloud.yandex.net:443"
    assert configs["gigachat"].base_url == "https://api.giga.chat/v1"
    assert configs["gigachat"].oauth_url == "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"


def test_gigachat_ca_bundle_is_operator_configurable(monkeypatch):
    from visual_provider_gateway.engine import provider_configs

    monkeypatch.setenv("GIGACHAT_CA_BUNDLE_FILE", "/etc/ssl/private/gigachat-root.pem")
    config = provider_configs()["gigachat"]
    assert config.ca_bundle_file == "/etc/ssl/private/gigachat-root.pem"
    assert config.safe_dict()["ca_bundle_configured"] is True
    assert "/etc/ssl/private" not in repr(config.safe_dict())


def test_submit_preserves_safe_http_failure_code_without_provider_body(monkeypatch):
    from visual_provider_gateway.engine import VisualCreativeEngine

    class BrokenProvider:
        def configured(self, kind):
            return True

        def submit(self, brief):
            raise providers.ProviderTransportError("http_400")

    monkeypatch.setattr("visual_provider_gateway.engine.provider_order", lambda *_args, **_kwargs: ("yandexart",))
    monkeypatch.setattr("visual_provider_gateway.engine.build_provider", lambda _name: BrokenProvider())

    job = VisualCreativeEngine(enabled=True).submit(CreativeBrief(kind="image", prompt="x"))

    assert job.status == "failed"
    assert job.error_code == "visual_provider_submit_http_400"
    assert job.provider_payload == {
        "attempts": ("yandexart:visual_provider_submit_http_400",),
    }


def test_submit_normalizes_ambiguous_timeout_and_does_not_failover(monkeypatch):
    from visual_provider_gateway.engine import VisualCreativeEngine
    from visual_provider_gateway.models import CreativeJob

    calls = []

    class BrokenProvider:
        def configured(self, kind):
            return True

        def submit(self, brief):
            calls.append("broken")
            raise providers.ProviderTransportError("TimeoutError")

    class SecondProvider:
        def configured(self, kind):
            return True

        def submit(self, brief):
            calls.append("second")
            return CreativeJob(provider="second", kind=brief.kind, status="queued", external_id="j2")

    monkeypatch.setattr("visual_provider_gateway.engine.provider_order", lambda *_args, **_kwargs: ("broken", "second"))
    monkeypatch.setattr(
        "visual_provider_gateway.engine.build_provider",
        lambda name: BrokenProvider() if name == "broken" else SecondProvider(),
    )
    monkeypatch.delenv("VISUAL_ALLOW_PROVIDER_FAILOVER_AFTER_ERROR", raising=False)

    job = VisualCreativeEngine(enabled=True).submit(CreativeBrief(kind="image", prompt="x"))

    assert job.error_code == "visual_provider_submit_timeout"
    assert calls == ["broken"]


def test_submit_never_exposes_unstructured_transport_error_text(monkeypatch):
    from visual_provider_gateway.engine import VisualCreativeEngine

    secret_marker = "super-secret-provider-body"

    class BrokenProvider:
        def configured(self, kind):
            return True

        def submit(self, brief):
            raise providers.ProviderTransportError(f"upstream rejected token={secret_marker}")

    monkeypatch.setattr("visual_provider_gateway.engine.provider_order", lambda *_args, **_kwargs: ("yandexart",))
    monkeypatch.setattr("visual_provider_gateway.engine.build_provider", lambda _name: BrokenProvider())

    job = VisualCreativeEngine(enabled=True).submit(CreativeBrief(kind="image", prompt="x"))
    rendered = repr((job.error_code, job.provider_payload))

    assert job.error_code == "visual_provider_submit_transport"
    assert secret_marker not in rendered


def test_yandexart_uses_current_alice_images_api(monkeypatch, tmp_path):
    monkeypatch.setenv("YANDEX_ART_PIPELINE", "images")
    from visual_provider_gateway.providers import YandexArtProvider

    observed = {}
    encoded = base64.b64encode(b"current-image").decode("ascii")

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0, ca_bundle_file=""):
        observed.update({"method": method, "url": url, "headers": headers, "payload": payload})
        return {"data": [{"b64_json": encoded}]}

    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    monkeypatch.setenv("YANDEX_API_KEY", "test")
    monkeypatch.setenv("VISUAL_TRANSIENT_OUTPUT_REQUIRED", "1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net",
            api_key="test",
            folder_id="folder",
            model_image="art://folder/aliceai-image-art-3.0",
            output_dir=str(tmp_path / "visual"),
        )
    )
    job = provider.submit(
        CreativeBrief(kind="image", prompt="new sink advertising image", aspect_ratio="16:9")
    )

    assert observed["url"].endswith("/v1/images/generations")
    assert observed["headers"] == {
        "Authorization": "Api-Key test",
        "OpenAI-Project": "folder",
    }
    assert observed["payload"] == {
        "model": "art://folder/aliceai-image-art-3.0",
        "prompt": "new sink advertising image",
        "size": "1536x1024",
    }
    assert job.status == "succeeded"
    assert job.model == "art://folder/aliceai-image-art-3.0"
    assert job.provider_payload["transport"] == "openai_compat"

def test_stored_visual_uses_actual_image_signature_for_mime_and_suffix(tmp_path):
    job = CreativeJob(
        provider="yandexart",
        kind="image",
        status="succeeded",
        external_id="image-signature-1",
        mime_type="image/png",
    )
    stored = providers._store_asset(
        ProviderConfig(name="yandexart", output_dir=str(tmp_path)),
        job,
        b"\xff\xd8\xff\xe0jpeg-payload",
    )

    assert stored.mime_type == "image/jpeg"
    assert Path(stored.asset_path).suffix == ".jpg"
    assert Path(stored.asset_path).read_bytes().startswith(b"\xff\xd8\xff")


def test_yandexart_motion_video_renders_current_alice_keyframe(monkeypatch, tmp_path):
    from visual_provider_gateway.providers import YandexArtMotionVideoProvider, YandexArtProvider

    source = tmp_path / "keyframe.png"
    source.write_bytes(b"png")
    target = tmp_path / "motion.mp4"

    def fake_image_submit(_self, brief):
        return CreativeJob(
            provider="yandexart",
            kind="image",
            status="succeeded",
            external_id="image-1",
            model="art://folder/aliceai-image-art-3.0",
            mime_type="image/png",
            asset_path=str(source),
        )

    def fake_render(_config, **kwargs):
        assert kwargs["image_path"] == str(source)
        assert kwargs["duration_seconds"] == 7
        assert kwargs["aspect_ratio"] == "16:9"
        target.write_bytes(b"mp4")
        return str(target)

    monkeypatch.setattr(YandexArtProvider, "submit", fake_image_submit)
    monkeypatch.setattr(providers, "_render_motion_video", fake_render)
    provider = YandexArtMotionVideoProvider(
        ProviderConfig(
            name="yandexart_motion",
            api_key="test",
            folder_id="folder",
            model_image="art://folder/aliceai-image-art-3.0",
            output_dir=str(tmp_path),
        )
    )
    job = provider.submit(
        CreativeBrief(
            kind="video",
            prompt="new sink advertising keyframe",
            duration_seconds=7,
            aspect_ratio="16:9",
        )
    )

    assert job.status == "succeeded"
    assert job.provider == "yandexart_motion"
    assert job.kind == "video"
    assert job.mime_type == "video/mp4"
    assert job.asset_path == str(target)
    assert source.exists() is False


def test_yandex_model_candidate_skips_deprecated_model_and_uses_current_alice(monkeypatch, tmp_path):
    monkeypatch.setenv("YANDEX_ART_PIPELINE", "images")
    from visual_provider_gateway.providers import YandexArtProvider

    calls = []
    encoded = base64.b64encode(b"current-image").decode("ascii")

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0, ca_bundle_file=""):
        calls.append((url, payload["model"]))
        return {"data": [{"b64_json": encoded}]}

    monkeypatch.setenv(
        "YANDEX_ART_MODEL_CANDIDATES",
        "art://folder/aliceai-image-art-3.0",
    )
    monkeypatch.delenv("YANDEX_ALLOW_DEPRECATED_ART_MODELS", raising=False)
    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    monkeypatch.setenv("YANDEX_API_KEY", "key")
    monkeypatch.setenv("VISUAL_TRANSIENT_OUTPUT_REQUIRED", "1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net",
            api_key="key",
            folder_id="folder",
            model_image="art://folder/yandex-art-2.0",
            output_dir=str(tmp_path / "visual"),
        )
    )

    job = provider.submit(CreativeBrief(kind="image", prompt="x"))

    assert calls == [
        (
            "https://ai.api.cloud.yandex.net/v1/images/generations",
            "art://folder/aliceai-image-art-3.0",
        )
    ]
    assert job.status == "succeeded"
    assert job.model == "art://folder/aliceai-image-art-3.0"

def test_yandex_image_circuit_also_blocks_motion_fallback(monkeypatch):
    import time

    from visual_provider_gateway.engine import VisualCreativeEngine

    engine = VisualCreativeEngine(enabled=True)
    engine._circuit_open_until["yandexart"] = time.monotonic() + 60

    assert engine._circuit_open("yandexart") is True
    assert engine._circuit_open("yandexart_motion") is True


def test_definitive_provider_rejection_opens_circuit_and_skips_next_request(monkeypatch):
    from visual_provider_gateway.engine import VisualCreativeEngine

    calls = []

    class BrokenProvider:
        def configured(self, kind):
            return True

        def submit(self, brief):
            calls.append("broken")
            raise providers.ProviderTransportError("http_403")

    class SecondProvider:
        def configured(self, kind):
            return True

        def submit(self, brief):
            calls.append("second")
            return CreativeJob(
                provider="second",
                kind=brief.kind,
                status="succeeded",
                model="m2",
            )

    monkeypatch.setattr(
        "visual_provider_gateway.engine.provider_order",
        lambda *_args, **_kwargs: ("broken", "second"),
    )
    monkeypatch.setattr(
        "visual_provider_gateway.engine.build_provider",
        lambda name: BrokenProvider() if name == "broken" else SecondProvider(),
    )
    engine = VisualCreativeEngine(enabled=True)

    first = engine.submit(CreativeBrief(kind="image", prompt="x"))
    second = engine.submit(CreativeBrief(kind="image", prompt="y"))

    assert first.provider == "second"
    assert second.provider == "second"
    assert calls == ["broken", "second", "second"]
    runtime = engine.runtime_snapshot()
    assert "broken" in runtime["circuits_open_seconds"]


def test_visual_provider_gateway_image_contains_ffmpeg_contract():
    from pathlib import Path

    dockerfile = Path("visual_provider_gateway/Dockerfile").read_text(encoding="utf-8")
    assert "apt-get install -y --no-install-recommends ffmpeg" in dockerfile

def test_yandexart_retries_with_renewable_iam_after_static_auth_rejection(monkeypatch, tmp_path):
    monkeypatch.setenv("YANDEX_ART_PIPELINE", "images")
    from visual_provider_gateway.providers import ProviderTransportError, YandexArtProvider

    calls = []
    encoded = base64.b64encode(b"renewed-image").decode("ascii")

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0):
        authorization = str((headers or {}).get("Authorization") or "")
        calls.append((method, url, authorization))
        if authorization.startswith("Api-Key "):
            raise ProviderTransportError("http_403")
        assert authorization == "Bearer renewable-iam-token"
        assert url.endswith("/v1/images/generations")
        return {"data": [{"b64_json": encoded}]}

    monkeypatch.setenv("YANDEX_API_KEY", "expired-or-insufficient-key")
    monkeypatch.delenv("YANDEX_ART_IAM_TOKEN", raising=False)
    monkeypatch.setenv("VISUAL_TRANSIENT_OUTPUT_REQUIRED", "1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    monkeypatch.setattr(
        providers,
        "get_yandex_art_iam_token",
        lambda: YandexIamTokenResult(
            configured=True,
            available=True,
            token="renewable-iam-token",
            auth_mode="authorized_key",
        ),
    )
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net:443",
            api_key="expired-or-insufficient-key",
            model_image="art://folder/aliceai-image-art-3.0",
            folder_id="folder",
            output_dir=str(tmp_path / "visual"),
        )
    )

    result = provider.submit(CreativeBrief(kind="image", prompt="hedgehog"))

    assert result.status == "succeeded"
    assert result.provider_payload["transport"] == "openai_compat"
    assert any(auth.startswith("Api-Key ") for _, _, auth in calls)
    assert calls[-1][2] == "Bearer renewable-iam-token"

def test_yandexart_does_not_switch_credentials_after_ambiguous_failure(monkeypatch):
    from visual_provider_gateway.providers import ProviderTransportError, YandexArtProvider

    renewable_calls = 0

    def renewable():
        nonlocal renewable_calls
        renewable_calls += 1
        return YandexIamTokenResult(
            configured=True,
            available=True,
            token="renewable-iam-token",
            auth_mode="authorized_key",
        )

    monkeypatch.setenv("YANDEX_API_KEY", "primary-key")
    monkeypatch.setattr(
        providers,
        "_json_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProviderTransportError("TimeoutError")
        ),
    )
    monkeypatch.setattr(providers, "get_yandex_art_iam_token", renewable)
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net:443",
            api_key="primary-key",
            model_image="art://folder/aliceai-image-art-3.0",
            folder_id="folder",
        )
    )

    with pytest.raises(ProviderTransportError, match="TimeoutError"):
        provider.submit(CreativeBrief(kind="image", prompt="hedgehog"))

    assert renewable_calls == 0


def test_yandexart_can_be_configured_by_renewable_iam_only(monkeypatch, tmp_path):
    monkeypatch.setenv("YANDEX_ART_PIPELINE", "images")
    from visual_provider_gateway.providers import YandexArtProvider

    encoded = base64.b64encode(b"renewable-only-image").decode("ascii")
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_ART_IAM_TOKEN", raising=False)
    monkeypatch.setenv("VISUAL_TRANSIENT_OUTPUT_REQUIRED", "1")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(providers, "yandex_art_renewable_auth_configured", lambda: True)
    monkeypatch.setattr(
        providers,
        "get_yandex_art_iam_token",
        lambda: YandexIamTokenResult(
            configured=True,
            available=True,
            token="renewable-only-token",
            auth_mode="authorized_key",
        ),
    )
    monkeypatch.setattr(
        providers,
        "_json_request",
        lambda *_args, **_kwargs: {"data": [{"b64_json": encoded}]},
    )
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net:443",
            api_key="",
            model_image="art://folder/aliceai-image-art-3.0",
            folder_id="folder",
            output_dir=str(tmp_path / "visual"),
        )
    )

    assert provider.configured("image") is True
    result = provider.submit(CreativeBrief(kind="image", prompt="hedgehog"))
    assert result.status == "succeeded"
    assert result.provider_payload["transport"] == "openai_compat"
    assert Path(result.asset_path).read_bytes() == b"renewable-only-image"
