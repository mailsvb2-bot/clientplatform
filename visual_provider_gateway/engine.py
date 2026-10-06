from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from dataclasses import replace

from .models import CreativeBrief, CreativeJob, ProviderConfig
from .prompt_adapter import PROMPT_ADAPTER_VERSION, adapt_visual_brief_for_provider
from .providers import (
    CreativeProvider,
    GigaChatImageProvider,
    OpenAIVisualProvider,
    ProviderTransportError,
    RunwayVisualProvider,
    SelfHostedVisualProvider,
    YandexArtMotionVideoProvider,
    YandexArtProvider,
)
from .yandex_model_catalog import get_yandex_model_catalog

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


def _yandex_orchestrator_model(folder_id: str) -> str:
    default = f"gpt://{folder_id}/aliceai-llm/latest" if folder_id else ""
    raw = str(_env("YANDEX_IMAGE_ORCHESTRATOR_MODEL", default) or "").strip().rstrip("/")
    if re.fullmatch(r"gpt://[^/]+/aliceai-llm", raw):
        return raw + "/latest"
    return raw


def provider_configs() -> dict[str, ProviderConfig]:
    timeout = _timeout()
    output_dir = _output_dir()
    max_json = _limit("VISUAL_MAX_JSON_BYTES", 32 * 1024 * 1024, minimum=64 * 1024, maximum=64 * 1024 * 1024)
    max_media = _limit("VISUAL_MAX_MEDIA_BYTES", 256 * 1024 * 1024, minimum=1024 * 1024, maximum=1024 * 1024 * 1024)
    yandex_folder = _env("YANDEX_ART_FOLDER_ID", _env("YANDEX_FOLDER_ID", "")).strip()
    return {
        "yandexart": ProviderConfig(
            name="yandexart",
            base_url=_env("YANDEX_ART_BASE_URL", "https://ai.api.cloud.yandex.net:443"),
            api_key=_env("YANDEX_API_KEY", _env("YANDEX_ART_IAM_TOKEN", "")),
            model_image=_env("YANDEX_ART_MODEL_URI", f"art://{yandex_folder}/aliceai-image-art-3.0" if yandex_folder else ""),
            model_orchestrator=_yandex_orchestrator_model(yandex_folder),
            folder_id=yandex_folder,
            timeout_seconds=timeout,
            max_json_bytes=max_json,
            max_media_bytes=max_media,
            output_dir=output_dir,
        ),
        "yandexart_motion": ProviderConfig(
            name="yandexart_motion",
            base_url=_env("YANDEX_ART_BASE_URL", "https://ai.api.cloud.yandex.net:443"),
            api_key=_env("YANDEX_API_KEY", _env("YANDEX_ART_IAM_TOKEN", "")),
            model_image=_env("YANDEX_ART_MODEL_URI", f"art://{yandex_folder}/aliceai-image-art-3.0" if yandex_folder else ""),
            model_orchestrator=_env("YANDEX_IMAGE_ORCHESTRATOR_MODEL", f"gpt://{yandex_folder}/aliceai-llm" if yandex_folder else ""),
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
        "selfhosted_backup": ProviderConfig(
            name="selfhosted_backup",
            base_url=_env("VISUAL_SELFHOST_BACKUP_BASE_URL", ""),
            api_key=_env("VISUAL_SELFHOST_BACKUP_TOKEN", ""),
            model_image=_env("VISUAL_SELFHOST_BACKUP_IMAGE_MODEL", ""),
            model_video=_env("VISUAL_SELFHOST_BACKUP_VIDEO_MODEL", ""),
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
    if normalized == "yandexart_motion":
        return YandexArtMotionVideoProvider(cfg)
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
            order = _csv("VISUAL_RU_VIDEO_ORDER", "selfhosted,selfhosted_backup,yandexart_motion")
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


def _video_provider_mode(name: str) -> str:
    """Classify native moving-scene generators vs keyframe motion fallback."""

    return "motion" if str(name or "").strip().lower() == "yandexart_motion" else "native"


def _prefer_native_video_order(order: tuple[str, ...]) -> tuple[str, ...]:
    native = tuple(name for name in order if _video_provider_mode(name) == "native")
    motion = tuple(name for name in order if _video_provider_mode(name) == "motion")
    if _truthy("VISUAL_VIDEO_MOTION_PRIMARY", "0"):
        return tuple(dict.fromkeys((*motion, *native)))
    return tuple(dict.fromkeys((*native, *motion)))


def provider_order(kind: str, country_code: str = "", preferred_provider: str = "") -> tuple[str, ...]:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {"image", "video"}:
        raise ValueError("visual kind must be image or video")
    policy = _policy_order(normalized_kind, country_code)
    if normalized_kind == "video":
        policy = _prefer_native_video_order(policy)
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


_KNOWN_MODEL_LIFECYCLE = {
    "yandex-art-2.0": {
        "deprecated_at": "2026-09-07",
        "replacement": "aliceai-image-art-3.0",
    },
    "yandex-art/latest": {
        "deprecated_at": "2026-09-07",
        "replacement": "aliceai-image-art-3.0",
    },
}


def _model_lifecycle_overrides() -> dict[str, dict[str, str]]:
    raw = str(os.getenv("VISUAL_MODEL_LIFECYCLE_JSON", "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for key, value in parsed.items():
        if not isinstance(value, dict):
            continue
        model = str(key or "").strip()
        if not model:
            continue
        result[model] = {
            "deprecated_at": str(value.get("deprecated_at") or "").strip(),
            "replacement": str(value.get("replacement") or "").strip(),
        }
    return result


def _model_lifecycle(model_uri: str) -> dict[str, object]:
    uri = str(model_uri or "").strip()
    model_id = uri.rsplit("/", 1)[-1] if uri else ""
    lifecycle_key = model_id
    for known in _KNOWN_MODEL_LIFECYCLE:
        if uri.endswith("/" + known) or uri == known:
            lifecycle_key = known
            break
    metadata = dict(_KNOWN_MODEL_LIFECYCLE.get(lifecycle_key) or {})
    overrides = _model_lifecycle_overrides()
    override = (
        overrides.get(uri)
        or overrides.get(lifecycle_key)
        or overrides.get(model_id)
    )
    if override:
        metadata.update(override)
    deprecated_at = str(metadata.get("deprecated_at") or "").strip()
    days_remaining: int | None = None
    status = "active"
    if deprecated_at:
        try:
            deadline = date.fromisoformat(deprecated_at)
            days_remaining = (deadline - date.today()).days
            status = "deprecated" if days_remaining < 0 else "deprecating"
        except ValueError:
            status = "unknown"
    return {
        "model": uri,
        "model_id": model_id,
        "deprecated_at": deprecated_at,
        "days_remaining": days_remaining,
        "replacement": str(metadata.get("replacement") or ""),
        "status": status,
    }


def _yandex_image_api_family() -> str:
    raw = str(_env("YANDEX_ART_PIPELINE", "images") or "images").strip().lower()
    if raw in {"images", "direct", "openai_images"}:
        return "openai_images"
    return "responses_image_generation"


def provider_snapshot(country_code: str = "") -> dict[str, object]:
    configs = provider_configs()
    yandex = configs["yandexart"]
    catalog = get_yandex_model_catalog(yandex)
    configured_video = configured_providers("video", country_code)
    configured_video_native = tuple(
        name for name in configured_video if _video_provider_mode(name) == "native"
    )
    configured_video_motion = tuple(
        name for name in configured_video if _video_provider_mode(name) == "motion"
    )
    video_generation_mode = (
        "native"
        if configured_video_native
        else "motion"
        if configured_video_motion
        else "unavailable"
    )
    return {
        "enabled": _truthy("VISUAL_CREATIVE_ENABLED", "0"),
        "country_code": str(country_code or _env("VISUAL_DEPLOYMENT_COUNTRY", "RU")).strip().upper(),
        "image_order": provider_order("image", country_code),
        "video_order": provider_order("video", country_code),
        "configured_image": configured_providers("image", country_code),
        "configured_video": configured_video,
        "configured_video_native": configured_video_native,
        "configured_video_motion": configured_video_motion,
        "video_generation_mode": video_generation_mode,
        "providers": {name: cfg.safe_dict() for name, cfg in configs.items()},
        "models": {
            "yandexart": {
                **_model_lifecycle(yandex.model_image),
                "api_family": _yandex_image_api_family(),
                "responses_required": _yandex_image_api_family() == "responses_image_generation",
                "direct_fallback_allowed": str(
                    os.getenv("YANDEX_ART_ALLOW_DIRECT_FALLBACK", "0") or "0"
                ).strip().lower() in {"1", "true", "yes", "on"},
                "orchestrator_model": yandex.model_orchestrator,
                "catalog_configured": catalog.configured,
                "catalog_available": catalog.available,
                "catalog_error": catalog.error_code,
                "configured_model_present": catalog.current_model_present,
                "available_art_models": catalog.art_models,
                "available_model_count": catalog.all_model_count,
                "candidate_count": len(
                    tuple(
                        dict.fromkeys(
                            part.strip()
                            for part in str(
                                os.getenv("YANDEX_ART_MODEL_CANDIDATES", "")
                                or yandex.model_image
                            ).split(",")
                            if part.strip()
                        )
                    )
                ),
            }
        },
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


def _should_stop_after_submit_failure(brief: CreativeBrief) -> bool:
    allow_failover = _truthy("VISUAL_ALLOW_PROVIDER_FAILOVER_AFTER_ERROR", "0")
    explicit_fallback = _truthy("VISUAL_EXPLICIT_PROVIDER_FALLBACK", "0")
    return not allow_failover or bool(brief.preferred_provider and not explicit_fallback)


class VisualCreativeEngine:
    def __init__(self, *, enabled: bool | None = None) -> None:
        self.enabled = _truthy("VISUAL_CREATIVE_ENABLED", "0") if enabled is None else bool(enabled)
        self._circuit_open_until: dict[str, float] = {}
        self._runtime: dict[str, dict[str, object]] = {"image": {}, "video": {}}

    @staticmethod
    def _circuit_seconds() -> int:
        return _limit(
            "VISUAL_PROVIDER_CIRCUIT_SECONDS",
            900,
            minimum=30,
            maximum=86400,
        )

    def _circuit_open(self, provider: str) -> bool:
        now = time.monotonic()
        if now < float(self._circuit_open_until.get(provider, 0.0) or 0.0):
            return True
        if provider == "yandexart_motion":
            return now < float(self._circuit_open_until.get("yandexart", 0.0) or 0.0)
        return False

    def _trip_circuit(self, provider: str, error_code: str) -> None:
        if error_code not in {
            "visual_provider_submit_http_401",
            "visual_provider_submit_http_403",
            "visual_provider_submit_http_404",
            "visual_provider_submit_http_410",
            "visual_provider_submit_connect_unreachable",
        }:
            return
        self._circuit_open_until[provider] = time.monotonic() + self._circuit_seconds()

    def _record_runtime(
        self,
        *,
        kind: str,
        provider: str,
        model: str = "",
        error_code: str = "",
        attempts: tuple[str, ...] = (),
    ) -> None:
        self._runtime[kind] = {
            "provider": provider,
            "model": model,
            "error_code": error_code,
            "attempts": attempts,
            "failover": bool(attempts and provider not in {"", "none"}),
            "updated_at_epoch": int(time.time()),
        }

    def runtime_snapshot(self) -> dict[str, object]:
        now = time.monotonic()
        circuits = {
            provider: max(0, int(open_until - now))
            for provider, open_until in self._circuit_open_until.items()
            if open_until > now
        }
        return {
            "image": dict(self._runtime.get("image") or {}),
            "video": dict(self._runtime.get("video") or {}),
            "circuits_open_seconds": circuits,
        }

    def submit(self, brief: CreativeBrief) -> CreativeJob:
        normalized = _apply_visual_safety(brief.normalized())
        if not self.enabled:
            return CreativeJob(provider="none", kind=normalized.kind, status="failed", error_code="visual_creative_disabled")
        failures: list[str] = []
        submit_failure_code = ""
        order = provider_order(normalized.kind, normalized.country_code, normalized.preferred_provider)
        for name in order:
            if self._circuit_open(name):
                failures.append(f"{name}:circuit_open")
                continue
            try:
                provider = build_provider(name)
            except ValueError:
                failures.append(f"{name}:unknown")
                continue
            if not provider.configured(normalized.kind):
                failures.append(f"{name}:not_configured")
                continue

            failure: BaseException | None = None
            try:
                provider_brief = adapt_visual_brief_for_provider(
                    normalized,
                    provider=name,
                )
                job = provider.submit(provider_brief)
                job.provider_payload.setdefault(
                    "prompt_adapter_version",
                    PROMPT_ADAPTER_VERSION,
                )
                self._circuit_open_until.pop(name, None)
                self._record_runtime(
                    kind=normalized.kind,
                    provider=job.provider,
                    model=job.model,
                    attempts=tuple(failures),
                )
                return job
            except ProviderTransportError as exc:
                failure = exc
            except (ValueError, TypeError) as exc:
                failure = exc
            except OSError as exc:
                failure = exc

            submit_failure_code = _submit_failure_code(failure)
            failures.append(f"{name}:{submit_failure_code}")
            self._trip_circuit(name, submit_failure_code)
            # Only definitive pre-acceptance failures are safe for an automatic
            # paid-provider failover. Timeouts/5xx remain fail-closed because the
            # first provider may already have accepted and billed the job.
            definitive_rejection = submit_failure_code in {
                "visual_provider_submit_http_400",
                "visual_provider_submit_http_401",
                "visual_provider_submit_http_403",
                "visual_provider_submit_http_404",
                "visual_provider_submit_http_410",
                "visual_provider_submit_http_422",
                "visual_provider_submit_connect_unreachable",
            }
            safe_policy_failover = (
                definitive_rejection and not normalized.preferred_provider
            )
            if not safe_policy_failover and _should_stop_after_submit_failure(normalized):
                break
        final_code = submit_failure_code or "no_visual_provider_available"
        self._record_runtime(
            kind=normalized.kind,
            provider="none",
            error_code=final_code,
            attempts=tuple(failures),
        )
        return CreativeJob(
            provider="none",
            kind=normalized.kind,
            status="failed",
            error_code=final_code,
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


def _compiled_prompt_allows_readable_text(prompt: str) -> bool:
    folded = " ".join(str(prompt or "").casefold().split())
    return "readable text is explicitly part of the owner's concept" in folded


def _is_compiled_visual_prompt(prompt: str) -> bool:
    folded = " ".join(str(prompt or "").casefold().split())
    return "owner request, preserve its meaning exactly:" in folded


def _apply_visual_safety(brief: CreativeBrief) -> CreativeBrief:
    """Presentation-only constraints; never contradict the frozen scene contract."""
    rules = [
        "No watermarks.",
        "Do not invent brand logos or certifications.",
        "Keep important subjects away from the outer 8 percent safe-area edges.",
    ]
    readable_text_allowed = (
        _truthy("VISUAL_ALLOW_MODEL_TEXT", "0")
        or _compiled_prompt_allows_readable_text(brief.prompt)
    )
    if not readable_text_allowed:
        # Typography/copy-space is owned by the compiled style contract. Do not
        # manufacture blank bands merely because generated text is forbidden.
        rules.append(
            "No readable text, letters, captions or UI in the generated pixels."
        )
    if brief.brand_context and not _is_compiled_visual_prompt(brief.prompt):
        # Compiled ClientPlatform prompts already carry bounded business grounding
        # plus exact rules for whether any wording may become visible. Re-appending
        # raw brand context here used to make names look like requested lettering.
        rules.append(
            "Use the supplied brand context only for visual direction and factual "
            "grounding. Do not render brand-context wording as visible text unless "
            "the prompt explicitly requests that exact wording. Brand direction: "
            + brief.brand_context
        )
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
