from __future__ import annotations

import base64
from pathlib import Path

import pytest

from visual_provider_gateway import providers
from visual_provider_gateway.engine import provider_order, provider_snapshot
from visual_provider_gateway.models import CreativeBrief, CreativeJob, ProviderConfig
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
        "VISUAL_VIDEO_MOTION_PRIMARY",
    ):
        monkeypatch.delenv(name, raising=False)


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
    from visual_provider_gateway.providers import YandexArtProvider

    observed = {}
    encoded = base64.b64encode(b"png-bytes").decode("ascii")

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0, ca_bundle_file=""):
        observed.update({"method": method, "url": url, "headers": headers, "payload": payload})
        return {"data": [{"b64_json": encoded}]}

    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net",
            api_key="test",
            folder_id="folder",
            model_image="art://folder/aliceai-image-art-3.0",
            output_dir=str(tmp_path),
        )
    )
    job = provider.submit(
        CreativeBrief(kind="image", prompt="new sink advertising image", aspect_ratio="16:9")
    )

    assert observed["url"].endswith("/v1/images/generations")
    assert observed["headers"]["OpenAI-Project"] == "folder"
    assert observed["payload"]["model"] == "art://folder/aliceai-image-art-3.0"
    assert observed["payload"]["size"] == "1536x1024"
    assert job.status == "succeeded"
    assert job.mime_type == "image/png"
    assert Path(job.asset_path).read_bytes() == b"png-bytes"


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


def test_yandex_model_candidate_failover_after_deprecated_model(monkeypatch, tmp_path):
    from visual_provider_gateway.providers import YandexArtProvider

    calls = []
    encoded = base64.b64encode(b"new-model").decode("ascii")

    def fake_json_request(method, url, *, headers=None, payload=None, timeout=30, max_bytes=0, ca_bundle_file=""):
        calls.append(payload["model"])
        if payload["model"].endswith("/yandex-art-2.0"):
            raise providers.ProviderTransportError("http_403")
        return {"data": [{"b64_json": encoded}]}

    monkeypatch.setenv(
        "YANDEX_ART_MODEL_CANDIDATES",
        "art://folder/aliceai-image-art-3.0",
    )
    monkeypatch.setattr(providers, "_json_request", fake_json_request)
    provider = YandexArtProvider(
        ProviderConfig(
            name="yandexart",
            base_url="https://ai.api.cloud.yandex.net",
            api_key="key",
            folder_id="folder",
            model_image="art://folder/yandex-art-2.0",
            output_dir=str(tmp_path),
        )
    )

    job = provider.submit(CreativeBrief(kind="image", prompt="x"))

    assert calls == [
        "art://folder/yandex-art-2.0",
        "art://folder/aliceai-image-art-3.0",
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