from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

_TRUE = frozenset({"1", "true", "yes", "on"})
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}")
_SECRET_REF_RE = re.compile(r"secret://env/(CLIENTPLATFORM_SECRET_SALES_AI_[A-Z0-9_]{3,96})")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
_SUPPORTED_PROVIDERS = frozenset({"deepseek", "openai", "openai_compatible"})
_DEEPSEEK_DEPRECATED_MODELS = frozenset({"deepseek-chat", "deepseek-reasoner"})
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_PROVIDER = "deepseek"
_DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
_DEFAULT_SECRET_REFERENCES = {
    "deepseek": "secret://env/CLIENTPLATFORM_SECRET_SALES_AI_DEEPSEEK_API_KEY",
    "openai": "secret://env/CLIENTPLATFORM_SECRET_SALES_AI_OPENAI_API_KEY",
    "openai_compatible": "secret://env/CLIENTPLATFORM_SECRET_SALES_AI_API_KEY",
}


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in _TRUE


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = str(env.get(name, default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float(env: Mapping[str, str], name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = str(env.get(name, default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def normalize_sales_ai_provider(value: str) -> str:
    provider = str(value or "").strip().lower()
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError("CLIENTPLATFORM_SALES_AI_PROVIDER must be deepseek, openai or openai_compatible")
    return provider


def sales_ai_secret_env_name(reference: str) -> str:
    match = _SECRET_REF_RE.fullmatch(str(reference or "").strip())
    if match is None:
        raise ValueError(
            "CLIENTPLATFORM_SALES_AI_API_KEY_REFERENCE must use a dedicated "
            "secret://env/CLIENTPLATFORM_SECRET_SALES_AI_* reference"
        )
    return match.group(1)


def _normalize_host(value: str) -> str:
    host = str(value or "").strip().lower().rstrip(".")
    if not host or not _HOST_RE.fullmatch(host):
        raise ValueError("Sales AI provider host is invalid")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Sales AI provider host must not be localhost")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host
    if not address.is_global:
        raise ValueError("Sales AI provider IP must be globally routable")
    return host


def _allowed_hosts(env: Mapping[str, str]) -> frozenset[str]:
    raw = str(env.get("CLIENTPLATFORM_SALES_AI_ALLOWED_HOSTS", "") or "")
    values = {_normalize_host(item) for item in raw.split(",") if item.strip()}
    return frozenset(values)


def _validate_endpoint(*, provider: str, base_url: str, allow_custom: bool, allowed_hosts: frozenset[str]) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("CLIENTPLATFORM_SALES_AI_BASE_URL must be an absolute credential-free HTTPS URL")
    host = _normalize_host(parsed.hostname)
    if provider == "deepseek":
        if host != "api.deepseek.com" or parsed.path.rstrip("/") != "":
            raise ValueError("DeepSeek Sales AI must use the official https://api.deepseek.com endpoint")
        return _DEEPSEEK_BASE_URL
    if provider == "openai":
        if host != "api.openai.com" or parsed.path.rstrip("/") != "/v1":
            raise ValueError("OpenAI Sales AI must use the official https://api.openai.com/v1 endpoint")
        return _OPENAI_BASE_URL
    if not allow_custom:
        raise ValueError("openai_compatible requires CLIENTPLATFORM_SALES_AI_ALLOW_CUSTOM_ENDPOINT=1")
    if not allowed_hosts or host not in allowed_hosts:
        raise ValueError("openai_compatible provider host must be explicitly listed in CLIENTPLATFORM_SALES_AI_ALLOWED_HOSTS")
    return normalized


@dataclass(frozen=True, slots=True)
class SalesAIRuntimeConfig:
    enabled: bool
    provider: str
    model: str
    base_url: str
    api_key_reference: str
    request_timeout_seconds: float
    max_output_tokens: int
    max_message_chars: int
    worker_batch_size: int
    worker_interval_seconds: float
    worker_lock_ttl_seconds: int
    worker_max_attempts: int
    raw_message_ttl_hours: int
    analysis_ttl_days: int
    allow_custom_endpoint: bool = False
    allowed_hosts: frozenset[str] = frozenset()

    @property
    def provider_label(self) -> str:
        if self.provider == "deepseek":
            return "DeepSeek"
        if self.provider == "openai":
            return "OpenAI"
        return "совместимый AI-провайдер"

    @property
    def consent_target(self) -> str:
        # Bind consent to the actual external destination, not merely a brand name.
        return f"{self.provider}:{self.base_url.lower().rstrip('/')}"

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "SalesAIRuntimeConfig":
        env = os.environ if environment is None else environment
        enabled = _bool(env, "CLIENTPLATFORM_SALES_AI_ENABLED", False)
        provider = normalize_sales_ai_provider(str(env.get("CLIENTPLATFORM_SALES_AI_PROVIDER", _DEFAULT_PROVIDER) or _DEFAULT_PROVIDER))
        allow_custom = _bool(env, "CLIENTPLATFORM_SALES_AI_ALLOW_CUSTOM_ENDPOINT", False)
        allowed_hosts = _allowed_hosts(env)

        default_base = _DEEPSEEK_BASE_URL if provider == "deepseek" else _OPENAI_BASE_URL if provider == "openai" else ""
        raw_base = str(env.get("CLIENTPLATFORM_SALES_AI_BASE_URL", default_base) or "").strip()
        base_url = raw_base.rstrip("/") if raw_base else ""
        default_model = _DEFAULT_DEEPSEEK_MODEL if provider == "deepseek" else ""
        model = str(env.get("CLIENTPLATFORM_SALES_AI_MODEL", default_model) or "").strip()
        secret_reference = str(env.get("CLIENTPLATFORM_SALES_AI_API_KEY_REFERENCE", _DEFAULT_SECRET_REFERENCES[provider]) or "").strip()

        if enabled:
            if not _MODEL_RE.fullmatch(model):
                raise ValueError("CLIENTPLATFORM_SALES_AI_MODEL must be a stable model identifier")
            if provider == "deepseek" and model in _DEEPSEEK_DEPRECATED_MODELS:
                raise ValueError("configured DeepSeek model id is discontinued; choose a current API model")
            base_url = _validate_endpoint(provider=provider, base_url=base_url, allow_custom=allow_custom, allowed_hosts=allowed_hosts)
            sales_ai_secret_env_name(secret_reference)
        elif not base_url:
            base_url = default_base

        timeout = _float(env, "CLIENTPLATFORM_SALES_AI_TIMEOUT_SEC", 20.0, minimum=3.0, maximum=60.0)
        lock_ttl = _int(env, "CLIENTPLATFORM_SALES_AI_LOCK_TTL_SEC", 120, minimum=30, maximum=1800)
        if enabled and lock_ttl < int(timeout) + 15:
            raise ValueError("CLIENTPLATFORM_SALES_AI_LOCK_TTL_SEC must exceed provider timeout by at least 15 seconds")
        batch = _int(env, "CLIENTPLATFORM_SALES_AI_BATCH_SIZE", 1, minimum=1, maximum=1)
        return cls(
            enabled=enabled,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_reference=secret_reference,
            request_timeout_seconds=timeout,
            max_output_tokens=_int(env, "CLIENTPLATFORM_SALES_AI_MAX_OUTPUT_TOKENS", 900, minimum=100, maximum=4000),
            max_message_chars=_int(env, "CLIENTPLATFORM_SALES_AI_MAX_MESSAGE_CHARS", 6000, minimum=256, maximum=12000),
            worker_batch_size=batch,
            worker_interval_seconds=_float(env, "CLIENTPLATFORM_SALES_AI_INTERVAL_SEC", 1.0, minimum=0.1, maximum=60.0),
            worker_lock_ttl_seconds=lock_ttl,
            worker_max_attempts=_int(env, "CLIENTPLATFORM_SALES_AI_MAX_ATTEMPTS", 5, minimum=1, maximum=12),
            raw_message_ttl_hours=_int(env, "CLIENTPLATFORM_SALES_AI_RAW_MESSAGE_TTL_HOURS", 168, minimum=1, maximum=720),
            analysis_ttl_days=_int(env, "CLIENTPLATFORM_SALES_AI_ANALYSIS_TTL_DAYS", 90, minimum=1, maximum=365),
            allow_custom_endpoint=allow_custom,
            allowed_hosts=allowed_hosts,
        )


__all__ = ["SalesAIRuntimeConfig", "normalize_sales_ai_provider", "sales_ai_secret_env_name"]
