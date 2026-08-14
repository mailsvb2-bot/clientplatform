from __future__ import annotations

import pytest

from visual_provider_gateway import providers
from visual_provider_gateway.engine import provider_order, provider_snapshot
from visual_provider_gateway.models import CreativeBrief, ProviderConfig
from visual_provider_gateway.providers import SelfHostedVisualProvider


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
    ):
        monkeypatch.delenv(name, raising=False)


def test_ru_defaults_keep_global_clouds_out(monkeypatch):
    _clear_provider_routing(monkeypatch)
    assert provider_order("image", "RU") == ("yandexart", "gigachat", "selfhosted")
    assert provider_order("video", "RU") == ("selfhosted",)


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
            base_url="https://llm.api.cloud.yandex.net:443",
            api_key="test",
            model_image="art://folder/yandex-art/latest",
        )
    )
    assert provider.configured("image") is True


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
    assert configs["yandexart"].base_url == "https://llm.api.cloud.yandex.net:443"
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
