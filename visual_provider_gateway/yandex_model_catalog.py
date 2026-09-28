from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .models import ProviderConfig


@dataclass(frozen=True, slots=True)
class YandexModelCatalogSnapshot:
    configured: bool
    available: bool
    current_model_present: bool = False
    art_models: tuple[str, ...] = ()
    all_model_count: int = 0
    error_code: str = ""


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    snapshot: YandexModelCatalogSnapshot


_cache: dict[str, _CacheEntry] = {}
_refreshing: set[str] = set()
_cache_lock = threading.Lock()


def _limit(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, str(default)) or str(default)).strip())
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def _canonical_model_uri(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw.split("@", 1)[0]


def _catalog_api_key(config: ProviderConfig) -> str:
    dedicated = str(os.getenv("YANDEX_MODEL_CATALOG_API_KEY", "") or "").strip()
    return dedicated or str(config.api_key or "").strip()


def _authorization(config: ProviderConfig) -> str:
    dedicated = str(os.getenv("YANDEX_MODEL_CATALOG_API_KEY", "") or "").strip()
    explicit_catalog_scheme = str(
        os.getenv("YANDEX_MODEL_CATALOG_AUTH_SCHEME", "") or ""
    ).strip()
    if dedicated:
        # A dedicated catalog credential is independent from the generation
        # credential. API keys are the default catalog credential, so never
        # inherit a generation-only Bearer scheme implicitly.
        scheme = explicit_catalog_scheme or "Api-Key"
    else:
        scheme = explicit_catalog_scheme or str(
            os.getenv("YANDEX_ART_AUTH_SCHEME", "") or ""
        ).strip()
        if not scheme:
            scheme = (
                "Api-Key"
                if str(os.getenv("YANDEX_API_KEY", "") or "").strip()
                else "Bearer"
                if str(os.getenv("YANDEX_ART_IAM_TOKEN", "") or "").strip()
                else "Api-Key"
            )
    return f"{scheme} {_catalog_api_key(config)}"


def _cache_key(config: ProviderConfig) -> str:
    token_digest = hashlib.sha256(_catalog_api_key(config).encode("utf-8")).hexdigest()
    return "|".join(
        (
            str(config.base_url or "").rstrip("/"),
            str(config.folder_id or ""),
            token_digest,
            _canonical_model_uri(config.model_image),
        )
    )


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"yandex_models_http_{int(exc.code)}"
    if isinstance(exc, urllib.error.URLError):
        return f"yandex_models_transport_{type(exc.reason).__name__}"
    if isinstance(exc, TimeoutError):
        return "yandex_models_transport_TimeoutError"
    if isinstance(exc, OSError):
        return f"yandex_models_transport_{type(exc).__name__}"
    return "yandex_models_unavailable"


def _fetch(config: ProviderConfig) -> YandexModelCatalogSnapshot:
    if not _catalog_api_key(config) or not config.folder_id:
        return YandexModelCatalogSnapshot(configured=False, available=False)

    request = urllib.request.Request(
        str(config.base_url or "https://ai.api.cloud.yandex.net").rstrip("/") + "/v1/models",
        headers={
            "Authorization": _authorization(config),
            "Accept": "application/json",
            "OpenAI-Project": str(config.folder_id),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=min(max(int(config.timeout_seconds or 30), 3), 30)) as response:  # nosec B310 - fixed configured Yandex AI endpoint
            raw = response.read(_limit("YANDEX_MODEL_CATALOG_MAX_BYTES", 1024 * 1024, minimum=64 * 1024, maximum=8 * 1024 * 1024) + 1)
    except urllib.error.HTTPError as exc:
        return YandexModelCatalogSnapshot(
            configured=True,
            available=False,
            error_code=_safe_error(exc),
        )
    except urllib.error.URLError as exc:
        return YandexModelCatalogSnapshot(
            configured=True,
            available=False,
            error_code=_safe_error(exc),
        )
    except TimeoutError as exc:
        return YandexModelCatalogSnapshot(
            configured=True,
            available=False,
            error_code=_safe_error(exc),
        )
    except OSError as exc:
        return YandexModelCatalogSnapshot(
            configured=True,
            available=False,
            error_code=_safe_error(exc),
        )

    max_bytes = _limit(
        "YANDEX_MODEL_CATALOG_MAX_BYTES",
        1024 * 1024,
        minimum=64 * 1024,
        maximum=8 * 1024 * 1024,
    )
    if len(raw) > max_bytes:
        return YandexModelCatalogSnapshot(
            configured=True,
            available=False,
            error_code="yandex_models_response_too_large",
        )

    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return YandexModelCatalogSnapshot(
            configured=True,
            available=False,
            error_code="yandex_models_invalid_response",
        )
    except json.JSONDecodeError:
        return YandexModelCatalogSnapshot(
            configured=True,
            available=False,
            error_code="yandex_models_invalid_response",
        )
    if not isinstance(payload, dict):
        return YandexModelCatalogSnapshot(
            configured=True,
            available=False,
            error_code="yandex_models_invalid_response",
        )

    rows = payload.get("data")
    if not isinstance(rows, list):
        return YandexModelCatalogSnapshot(
            configured=True,
            available=False,
            error_code="yandex_models_invalid_response",
        )

    models: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_uri = str(row.get("id") or "").strip()
        if model_uri:
            models.append(model_uri)

    art_models = tuple(
        dict.fromkeys(
            sorted(
                model
                for model in models
                if _canonical_model_uri(model).startswith("art://")
            )
        )
    )
    current = _canonical_model_uri(config.model_image)
    present = bool(
        current
        and any(_canonical_model_uri(model) == current for model in art_models)
    )
    return YandexModelCatalogSnapshot(
        configured=True,
        available=True,
        current_model_present=present,
        art_models=art_models,
        all_model_count=len(models),
    )


def _refresh_catalog(key: str, config: ProviderConfig, ttl: int) -> None:
    try:
        snapshot = _fetch(config)
        with _cache_lock:
            _cache[key] = _CacheEntry(
                expires_at=time.monotonic() + ttl,
                snapshot=snapshot,
            )
    finally:
        with _cache_lock:
            _refreshing.discard(key)


def _ensure_refresh(key: str, config: ProviderConfig, ttl: int) -> None:
    with _cache_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)
    thread = threading.Thread(
        target=_refresh_catalog,
        args=(key, config, ttl),
        name="yandex-model-catalog-refresh",
        daemon=True,
    )
    thread.start()


def get_yandex_model_catalog(config: ProviderConfig) -> YandexModelCatalogSnapshot:
    if not _catalog_api_key(config) or not config.folder_id:
        return YandexModelCatalogSnapshot(configured=False, available=False)

    ttl = _limit("YANDEX_MODEL_CATALOG_TTL_SECONDS", 900, minimum=30, maximum=86400)
    key = _cache_key(config)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached.expires_at > now:
            return cached.snapshot
        stale = cached.snapshot if cached is not None else None

    # Advisory discovery must never delay /v1/providers or container readiness.
    # Refresh in the background and serve stale data while revalidating.
    _ensure_refresh(key, config, ttl)
    if stale is not None:
        return stale
    return YandexModelCatalogSnapshot(
        configured=True,
        available=False,
    )


def refresh_yandex_model_catalog(config: ProviderConfig) -> YandexModelCatalogSnapshot:
    """Synchronously refresh advisory catalog data outside health/readiness paths."""
    if not _catalog_api_key(config) or not config.folder_id:
        return YandexModelCatalogSnapshot(configured=False, available=False)
    ttl = _limit("YANDEX_MODEL_CATALOG_TTL_SECONDS", 900, minimum=30, maximum=86400)
    key = _cache_key(config)
    snapshot = _fetch(config)
    with _cache_lock:
        _cache[key] = _CacheEntry(
            expires_at=time.monotonic() + ttl,
            snapshot=snapshot,
        )
    return snapshot


def clear_yandex_model_catalog_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _refreshing.clear()


__all__ = [
    "YandexModelCatalogSnapshot",
    "clear_yandex_model_catalog_cache",
    "get_yandex_model_catalog",
    "refresh_yandex_model_catalog",
]
