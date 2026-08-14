from __future__ import annotations

import os
import re
import time
from dataclasses import replace

from .models import CreativeBrief, CreativeJob, ProviderConfig
from .providers import (
    CreativeProvider,
    GigaChatImageProvider,
    OpenAIVisualProvider,
    ProviderTransportError,
    RunwayVisualProvider,
    SelfHostedVisualProvider,
    YandexArtProvider,
)

_RU_COUNTRIES = {"RU", "RUS"}


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _truthy(name: str, default: str = "0") -> bool:
    return str(_env(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def _timeout() -> int:
    try:
        return max(3, min(int(_env("VISUAL_TIMEOUT_SECONDS", "30")), 300))
    except ValueError:
        return 30


def _limit(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def _output_dir() -> str:
    return _env("VISUAL_CREATIVE_OUTPUT_DIR", "data/visual_creatives")


def provider_configs() -> dict[str, ProviderConfig]:
    timeout = _timeout()
    output_dir = _output_dir()
    max_json = _limit("VISUAL_MAX_JSON_BYTES", 32 * 1024 * 1024, minimum=64 * 1024, maximum=64 * 1024 * 1024)
    max_media = _limit("VISUAL_MAX_MEDIA_BYTES", 256 * 1024 * 1024, minimum=1024 * 1024, maximum=1024 * 1024 * 1024)
    yandex_folder = _env("YANDEX_ART_FOLDER_ID", _env("YANDEX_FOLDER_ID", "")).strip()
    return {
        "yandexart": ProviderConfig(
            name="yandexart",
            base_url=_env("YANDEX_ART_BASE_URL", "https://llm.api.cloud.yandex.net:443"),
            api_key=_env("YANDEX_ART_IAM_TOKEN", _env("YANDEX_API_KEY", "")),
            model_image=_env("YANDEX_ART_MODEL_URI", f"art://{yandex_folder}/yandex-art/latest" if yandex_folder else ""),
            folder_id=yandex_folder,
            timeout_seconds=timeout,
            max_json_bytes=max_json,
            max_media_bytes=max_media,
            output_dir=output_dir,
        ),
        "gigachat": ProviderConfig(
            name="gigachat",
            base_url=_env("VISUAL_GIGACHAT_BASE_URL", _env("GIGACHAT_BASE_URL", "https://api.giga.chat/v1")),
            credentials=_env("GIGACHAT_CREDENTIALS", ""),
            oauth_url=_env("VISUAL_GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"),
            scope=_env("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            ca_bundle_file=_env("VISUAL_GIGACHAT_CA_BUNDLE_FILE", _env("GIGACHAT_CA_BUNDLE_FILE", "")),
            model_image=_env("VISUAL_GIGACHAT_MODEL", _env("GIGACHAT_MODEL", "GigaChat-2-Pro")),
            timeout_seconds=timeout,
            max_json_bytes=max_json,
            max_media_bytes=max_media,
            output_dir=output_dir,
        ),
        "openai": ProviderConfig(
            name="openai",
            base_url=_env("VISUAL_OPENAI_BASE_URL", _env("OPENAI_BASE_URL", "https://api.openai.com/v1")),
            api_key=_env("VISUAL_OPENAI_API_KEY", _env("OPENAI_API_KEY", "")),
            model_image=_env("VISUAL_OPENAI_IMAGE_MODEL", "gpt-image-2"),
            model_video=_env("VISUAL_OPENAI_VIDEO_MODEL", ""),
            timeout_seconds=timeout,
            max_json_bytes=max_json,
            max_media_bytes=max_media,
            output_dir=output_dir,
        ),
        "runway": ProviderConfig(
            name="runway",
            base_url=_env("RUNWAY_BASE_URL", "https://api.dev.runwayml.com/v1"),
            api_key=_env("RUNWAYML_API_SECRET", _env("RUNWAY_API_KEY", "")),
            model_image=_env("RUNWAY_IMAGE_MODEL", "gen4_image"),
            model_video=_env("RUNWAY_VIDEO_MODEL", "gen4.5"),
            timeout_seconds=timeout,
            max_json_bytes=max_json,
            max_media_bytes=max_media,
            output_dir=output_dir,
        ),
        "selfhosted": ProviderConfig(
            name="selfhosted",
            base_url=_env("VISUAL_SELFHOST_BASE_URL", ""),
            api_key=_env("VISUAL_SELFHOST_TOKEN", ""),
            model_image=_env("VISUAL_SELFHOST_IMAGE_MODEL", ""),
            model_video=_env("VISUAL_SELFHOST_VIDEO_MODEL", ""),
            timeout_seconds=timeout,
            max_json_bytes=max_json,
            max_media_bytes=max_media,
            output_dir=output_dir,
        ),
    }


def build_provider(name: str) -> CreativeProvider:
    normalized = str(name or "").strip().lower()
    configs = provider_configs()
    if normalized not in configs:
        raise ValueError(f"unknown visual provider: {normalized}")
    cfg = configs[normalized]
    if normalized == "yandexart":
        return YandexArtProvider(cfg)
    if normalized == "gigachat":
        return GigaChatImageProvider(cfg)
    if normalized == "openai":
        return OpenAIVisualProvider(cfg)
    if normalized == "runway":
        return RunwayVisualProvider(cfg)
    return SelfHostedVisualProvider(cfg)


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = _env(name, default)
    return tuple(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))


def _country_order(kind: str, country: str) -> tuple[str, ...]:
    token = re.sub(r"[^A-Z0-9]", "", str(country or "").upper())
    if not token:
        return ()
    raw = _env(f"VISUAL_{token}_{kind.upper()}_ORDER", "").strip()
    return _csv(f"VISUAL_{token}_{kind.upper()}_ORDER", "") if raw else ()


def _policy_order(kind: str, country_code: str = "") -> tuple[str, ...]:
    env_explicit = _env("VISUAL_IMAGE_PROVIDER" if kind == "image" else "VISUAL_VIDEO_PROVIDER", "auto").strip().lower()
    if env_explicit and env_explicit != "auto":
        # Operator-level deployment configuration is authoritative. Request-level
        # overrides below are still constrained by the resulting deployment policy.
        return (env_explicit,)
    country = str(country_code or _env("VISUAL_DEPLOYMENT_COUNTRY", "RU")).strip().upper()
    country_specific = _country_order(kind, country)
    if country_specific:
        return country_specific
    if country in _RU_COUNTRIES:
        if kind == "image":
            order = _csv("VISUAL_RU_IMAGE_ORDER", "yandexart,gigachat,selfhosted")
        else:
            order = _csv("VISUAL_RU_VIDEO_ORDER", "selfhosted")
        if _truthy("VISUAL_ALLOW_GLOBAL_PROVIDERS_IN_RU", "0"):
            global_order = _csv(
                "VISUAL_GLOBAL_IMAGE_ORDER" if kind == "image" else "VISUAL_GLOBAL_VIDEO_ORDER",
                "openai,runway,selfhosted" if kind == "image" else "runway,selfhosted,openai",
            )
            order = tuple(dict.fromkeys((*order, *global_order)))
        return order
    return _csv(
        "VISUAL_GLOBAL_IMAGE_ORDER" if kind == "image" else "VISUAL_GLOBAL_VIDEO_ORDER",
        "openai,runway,selfhosted" if kind == "image" else "runway,selfhosted,openai",
    )


def provider_order(kind: str, country_code: str = "", preferred_provider: str = "") -> tuple[str, ...]:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {"image", "video"}:
        raise ValueError("visual kind must be image or video")
    policy = _policy_order(normalized_kind, country_code)
    explicit = str(preferred_provider or "").strip().lower()
    if not explicit or explicit == "auto":
        return policy
    if not _truthy("VISUAL_ALLOW_REQUEST_PROVIDER_OVERRIDE", "0"):
        raise ValueError("visual_provider_override_disabled")
    if explicit not in policy:
        raise ValueError("visual_provider_not_allowed_by_country_policy")
    return (explicit,)


def configured_providers(kind: str, country_code: str = "") -> tuple[str, ...]:
    names: list[str] = []
    for name in provider_order(kind, country_code):
        try:
            provider = build_provider(name)
        except ValueError:
            continue
        if provider.configured(kind):
            names.append(name)
    return tuple(names)


def provider_snapshot(country_code: str = "") -> dict[str, object]:
    configs = provider_configs()
    return {
        "enabled": _truthy("VISUAL_CREATIVE_ENABLED", "0"),
        "country_code": str(country_code or _env("VISUAL_DEPLOYMENT_COUNTRY", "RU")).strip().upper(),
        "image_order": provider_order("image", country_code),
        "video_order": provider_order("video", country_code),
        "configured_image": configured_providers("image", country_code),
        "configured_video": configured_providers("video", country_code),
        "providers": {name: cfg.safe_dict() for name, cfg in configs.items()},
    }


def _submit_failure_code(exc: BaseException) -> str:
    """Return a bounded provider-safe code without leaking provider response bodies."""
    if isinstance(exc, ProviderTransportError):
        raw = str(exc or "").strip()
        http_match = re.fullmatch(r"http_(\d{3})", raw)
        if http_match:
            return f"visual_provider_submit_http_{http_match.group(1)}"
        normalized = raw.casefold()
        if normalized in {"timeouterror", "timeout", "socket_timeout"}:
            return "visual_provider_submit_timeout"
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", normalized):
            return f"visual_provider_submit_{normalized}"
        return "visual_provider_submit_transport"
    if isinstance(exc, (ValueError, TypeError)):
        return "visual_provider_submit_invalid_request"
    return "visual_provider_submit_transport"


class VisualCreativeEngine:
    def __init__(self, *, enabled: bool | None = None) -> None:
        self.enabled = _truthy("VISUAL_CREATIVE_ENABLED", "0") if enabled is None else bool(enabled)

    def submit(self, brief: CreativeBrief) -> CreativeJob:
        normalized = _apply_visual_safety(brief.normalized())
        if not self.enabled:
            return CreativeJob(provider="none", kind=normalized.kind, status="failed", error_code="visual_creative_disabled")
        failures: list[str] = []
        submit_failure_code = ""
        order = provider_order(normalized.kind, normalized.country_code, normalized.preferred_provider)
        for name in order:
            try:
                provider = build_provider(name)
            except ValueError:
                failures.append(f"{name}:unknown")
                continue
            if not provider.configured(normalized.kind):
                failures.append(f"{name}:not_configured")
                continue
            try:
                return provider.submit(normalized)
            except (ProviderTransportError, ValueError, TypeError, OSError) as exc:
                submit_failure_code = _submit_failure_code(exc)
                failures.append(f"{name}:{submit_failure_code}")
                # A timed-out/failed POST can be ambiguous: the provider may have
                # accepted and billed the job even though we never received its ID.
                # Default to fail-closed instead of starting another paid provider
                # request. Operators may explicitly opt into that cost/risk tradeoff.
                allow_failover = _truthy("VISUAL_ALLOW_PROVIDER_FAILOVER_AFTER_ERROR", "0")
                explicit_fallback = _truthy("VISUAL_EXPLICIT_PROVIDER_FALLBACK", "0")
                if not allow_failover or (normalized.preferred_provider and not explicit_fallback):
                    break
        return CreativeJob(
            provider="none",
            kind=normalized.kind,
            status="failed",
            error_code=submit_failure_code or "no_visual_provider_available",
            provider_payload={"attempts": tuple(failures)},
        )

    def poll(self, job: CreativeJob) -> CreativeJob:
        if job.done:
            return job
        try:
            provider = build_provider(job.provider)
        except ValueError:
            job.status = "failed"
            job.error_code = "unknown_provider"
            return job
        try:
            refreshed = provider.poll(job)
            if refreshed.status != "failed":
                refreshed.error_code = ""
            return refreshed
        except ProviderTransportError as exc:
            raw = str(exc or "")
            match = re.fullmatch(r"http_(\d{3})", raw)
            code = int(match.group(1)) if match else 0
            terminal_http = 400 <= code < 500 and code not in {408, 409, 425, 429}
            if terminal_http:
                job.status = "failed"
                job.error_code = f"visual_provider_poll_http_{code}"
            else:
                # Poll is safe to retry: unlike submit, it does not start another
                # paid generation. Preserve the running job across transient I/O.
                job.error_code = "visual_provider_poll_transient"
            return job
        except (ValueError, TypeError, OSError):
            job.status = "failed"
            job.error_code = "visual_provider_poll_failed"
            return job

    def generate(self, brief: CreativeBrief, *, wait_seconds: int = 0, poll_interval: float = 2.0) -> CreativeJob:
        job = self.submit(brief)
        if job.done or wait_seconds <= 0:
            return job
        deadline = time.monotonic() + max(0, int(wait_seconds))
        while time.monotonic() < deadline and not job.done:
            time.sleep(max(0.2, min(float(poll_interval), 5.0)))
            job = self.poll(job)
        return job


def _apply_visual_safety(brief: CreativeBrief) -> CreativeBrief:
    """Presentation-only constraints; this function never chooses an offer/audience."""
    rules = [
        "No watermarks.",
        "Do not invent brand logos or certifications.",
        "Keep important subjects away from the outer 8 percent safe-area edges.",
    ]
    if not _truthy("VISUAL_ALLOW_MODEL_TEXT", "0"):
        rules.append("No readable text, letters, captions or UI in the generated pixels; leave clean negative space for real typography overlay.")
    if brief.brand_context:
        rules.append("Brand direction: " + brief.brand_context)
    prompt = brief.prompt.rstrip() + "\n\nProduction constraints: " + " ".join(rules)
    return replace(brief, prompt=prompt)


__all__ = [
    "VisualCreativeEngine",
    "build_provider",
    "configured_providers",
    "provider_configs",
    "provider_order",
    "provider_snapshot",
]
