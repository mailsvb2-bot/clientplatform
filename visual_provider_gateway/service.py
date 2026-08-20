from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import VisualCreativeEngine, provider_snapshot
from .models import CreativeBrief, CreativeJob
from .store import JobStore, StoredJob



_SAFE_EXPLICIT_RETRY_ERRORS = frozenset(
    {
        "no_visual_provider_available",
        "visual_creative_disabled",
        "visual_provider_submit_invalid_request",
        "visual_provider_submit_http_400",
        "visual_provider_submit_http_401",
        "visual_provider_submit_http_403",
        "visual_provider_submit_http_404",
        "visual_provider_submit_http_422",
    }
)

class VisualGatewayService:
    def __init__(self, *, store: JobStore | None = None, engine: VisualCreativeEngine | None = None) -> None:
        self.store = store or JobStore()
        self.engine = engine or VisualCreativeEngine()

    @staticmethod
    def _response(job: StoredJob) -> dict[str, Any]:
        return {
            "id": job.id,
            "provider": job.provider,
            "scope_id": job.scope_id,
            "kind": job.kind,
            "status": job.status,
            "model": job.model,
            "mime_type": job.mime_type,
            "error_code": job.error_code,
            "asset_ready": bool(job.asset_path and job.status == "succeeded"),
        }

    @staticmethod
    def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
        try:
            value = int(str(os.getenv(name, default)))
        except (TypeError, ValueError):
            return default
        return max(minimum, min(value, maximum))

    @classmethod
    def _client_daily_limit(cls, client_id: str) -> int:
        default = cls._env_int("VISUAL_GATEWAY_DAILY_JOB_LIMIT", 500, minimum=1, maximum=1_000_000)
        raw = str(os.getenv("VISUAL_GATEWAY_CLIENT_DAILY_LIMITS_JSON", "") or "").strip()
        if not raw:
            return default
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_visual_gateway_client_daily_limits_json") from exc
        if not isinstance(parsed, dict):
            raise ValueError("invalid_visual_gateway_client_daily_limits_json")
        selected = parsed.get(client_id, default)
        try:
            return max(1, min(int(selected), 1_000_000))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_visual_gateway_client_daily_limit") from exc

    @classmethod
    def _client_kind_daily_limit(cls, client_id: str, kind: str) -> int:
        visual_kind = str(kind or "").strip().lower()
        if visual_kind not in {"image", "video"}:
            raise ValueError("invalid_visual_kind")
        default = cls._env_int(
            "VISUAL_GATEWAY_DAILY_IMAGE_LIMIT" if visual_kind == "image" else "VISUAL_GATEWAY_DAILY_VIDEO_LIMIT",
            500 if visual_kind == "image" else 50,
            minimum=1,
            maximum=1_000_000,
        )
        env_name = (
            "VISUAL_GATEWAY_CLIENT_DAILY_IMAGE_LIMITS_JSON"
            if visual_kind == "image"
            else "VISUAL_GATEWAY_CLIENT_DAILY_VIDEO_LIMITS_JSON"
        )
        raw = str(os.getenv(env_name, "") or "").strip()
        if not raw:
            return default
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_visual_gateway_client_kind_daily_limits_json") from exc
        if not isinstance(parsed, dict):
            raise ValueError("invalid_visual_gateway_client_kind_daily_limits_json")
        selected = parsed.get(client_id, default)
        try:
            return max(1, min(int(selected), 1_000_000))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_visual_gateway_client_kind_daily_limit") from exc

    @classmethod
    def _client_active_limit(cls, client_id: str) -> int:
        del client_id
        return cls._env_int("VISUAL_GATEWAY_MAX_ACTIVE_JOBS_PER_CLIENT", 20, minimum=1, maximum=10_000)

    @staticmethod
    def _truthy_env(name: str, default: str = "0") -> bool:
        return str(os.getenv(name, default) or default).strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def _effective_country(cls, requested: object) -> str:
        deployment = str(os.getenv("VISUAL_DEPLOYMENT_COUNTRY", "RU") or "RU").strip().upper() or "RU"
        token = re.sub(r"[^A-Z0-9]", "", str(requested or "").strip().upper())
        if not token or token == deployment:
            return deployment
        if not cls._truthy_env("VISUAL_ALLOW_REQUEST_COUNTRY_OVERRIDE", "0"):
            return deployment
        allowed_raw = str(os.getenv("VISUAL_REQUEST_COUNTRY_ALLOWLIST", "") or "").strip()
        allowed = {re.sub(r"[^A-Z0-9]", "", part.strip().upper()) for part in allowed_raw.split(",") if part.strip()}
        if allowed and token not in allowed:
            raise ValueError("visual_request_country_not_allowed")
        return token

    def _assert_capacity(self, *, client_id: str, kind: str) -> None:
        since = self.store.utc_day_start_epoch()
        if self.store.count_since(client_id=client_id, since_epoch=since) > self._client_daily_limit(client_id):
            raise PermissionError("visual_gateway_daily_limit_reached")
        if self.store.count_since(client_id=client_id, since_epoch=since, kind=kind) > self._client_kind_daily_limit(client_id, kind):
            raise PermissionError(f"visual_gateway_daily_{kind}_limit_reached")
        if self.store.active_count(client_id=client_id) > self._client_active_limit(client_id):
            raise PermissionError("visual_gateway_active_job_limit_reached")

    @staticmethod
    def _reference_url(value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        enabled = str(os.getenv("VISUAL_ALLOW_REFERENCE_URLS", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            raise ValueError("visual_reference_urls_disabled")
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("invalid_visual_reference_url")
        allowed_raw = str(os.getenv("VISUAL_REFERENCE_URL_ALLOWED_HOSTS", "") or "").strip()
        allowed = {part.strip().casefold() for part in allowed_raw.split(",") if part.strip()}
        if not allowed or parsed.hostname.casefold() not in allowed:
            raise ValueError("visual_reference_host_not_allowed")
        return raw

    def submit(self, payload: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        scope_id = str(payload.get("scope_id") or "global").strip() or "global"
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        kind = str(payload.get("kind") or "image").strip().lower()
        effective_country = self._effective_country(payload.get("country_code"))

        fingerprint_payload = {
            key: payload.get(key)
            for key in (
                "kind", "prompt", "preferred_provider", "aspect_ratio",
                "duration_seconds", "negative_prompt", "reference_url", "brand_context", "seed", "scope_id"
            )
        }
        fingerprint_payload["country_code"] = effective_country
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        reserved, created = self.store.reserve(
            client_id=client_id,
            scope_id=scope_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            kind=kind,
        )
        if not created:
            if not self.store.rearm_failed(
                reserved.id,
                client_id=client_id,
                scope_id=scope_id,
                allowed_error_codes=_SAFE_EXPLICIT_RETRY_ERRORS,
            ):
                return self._response(
                    self.store.get(
                        reserved.id,
                        client_id=client_id,
                        scope_id=scope_id,
                    )
                )
            reserved = self.store.get(
                reserved.id,
                client_id=client_id,
                scope_id=scope_id,
            )

        try:
            self._assert_capacity(client_id=client_id, kind=kind)
            brief = CreativeBrief(
                kind=kind,  # type: ignore[arg-type]
                prompt=str(payload.get("prompt") or ""),
                country_code=effective_country,
                aspect_ratio=str(payload.get("aspect_ratio") or "1:1"),
                duration_seconds=int(payload.get("duration_seconds") or 5),
                negative_prompt=str(payload.get("negative_prompt") or ""),
                reference_url=self._reference_url(payload.get("reference_url")),
                preferred_provider=str(payload.get("preferred_provider") or ""),
                brand_context=str(payload.get("brand_context") or ""),
                seed=(None if payload.get("seed") in (None, "") else int(payload["seed"])),
                metadata={"client_id": client_id, "scope_id": scope_id},
            )
            wait = max(0, min(int(payload.get("wait_seconds") or 0), 60))
            job = self.engine.generate(brief, wait_seconds=wait)
        except PermissionError:
            self.store.update(
                reserved.id,
                client_id=client_id,
                scope_id=scope_id,
                provider="none",
                kind=kind,
                status="failed",
                error_code="visual_gateway_quota_rejected",
            )
            raise
        except (ValueError, TypeError):
            self.store.update(
                reserved.id,
                client_id=client_id,
                scope_id=scope_id,
                provider="none",
                kind=kind,
                status="failed",
                error_code="visual_gateway_submit_failed",
            )
            raise

        stored = self.store.update(
            reserved.id,
            client_id=client_id,
            scope_id=scope_id,
            provider=job.provider,
            kind=job.kind,
            status=job.status,
            provider_job_id=job.external_id,
            model=job.model,
            mime_type=job.mime_type,
            asset_path=job.asset_path,
            error_code=job.error_code,
        )
        return self._response(stored)

    def poll(self, gateway_id: str, *, client_id: str, scope_id: str) -> dict[str, Any]:
        scope = str(scope_id or "").strip()
        stored = self.store.get(gateway_id, client_id=client_id, scope_id=scope)
        if stored.status in {"succeeded", "failed"}:
            return self._response(stored)
        if not stored.provider:
            start_timeout = self._env_int("VISUAL_GATEWAY_START_RESERVATION_TIMEOUT_SECONDS", 180, minimum=30, maximum=3600)
            if int(time.time()) - stored.updated_at > start_timeout:
                stored = self.store.update(
                    stored.id,
                    client_id=client_id,
                    scope_id=scope,
                    provider="none",
                    kind=stored.kind,
                    status="failed",
                    error_code="visual_gateway_start_incomplete",
                )
            return self._response(stored)
        refreshed = self.engine.poll(
            CreativeJob(
                provider=stored.provider,
                kind=stored.kind,  # type: ignore[arg-type]
                status=stored.status,  # type: ignore[arg-type]
                external_id=stored.provider_job_id,
                model=stored.model,
                mime_type=stored.mime_type,
            )
        )
        updated = self.store.update(
            stored.id,
            client_id=client_id,
            scope_id=scope,
            provider=refreshed.provider,
            kind=refreshed.kind,
            status=refreshed.status,
            provider_job_id=refreshed.external_id,
            model=refreshed.model,
            mime_type=refreshed.mime_type,
            asset_path=refreshed.asset_path,
            error_code=refreshed.error_code,
        )
        return self._response(updated)

    def content_path(self, gateway_id: str, *, client_id: str, scope_id: str) -> tuple[Path, str]:
        stored = self.store.get(gateway_id, client_id=client_id, scope_id=scope_id)
        if stored.status != "succeeded" or not stored.asset_path:
            raise FileNotFoundError(gateway_id)
        output_root = Path(os.getenv("VISUAL_CREATIVE_OUTPUT_DIR", "data/visual_creatives")).expanduser().resolve()
        candidate = Path(stored.asset_path).expanduser().resolve()
        try:
            candidate.relative_to(output_root)
        except ValueError as exc:
            raise FileNotFoundError(gateway_id) from exc
        if not candidate.is_file():
            raise FileNotFoundError(gateway_id)
        return candidate, stored.mime_type or "application/octet-stream"

    def usage_snapshot(self, client_id: str) -> dict[str, Any]:
        client = str(client_id or "").strip()
        since = self.store.utc_day_start_epoch()
        resets_at = datetime.fromtimestamp(since + 86400, tz=timezone.utc)

        def counter(kind: str = "") -> dict[str, int]:
            limit = (
                self._client_daily_limit(client)
                if not kind
                else self._client_kind_daily_limit(client, kind)
            )
            used = self.store.count_since(
                client_id=client,
                since_epoch=since,
                kind=kind,
            )
            return {
                "used": used,
                "limit": limit,
                "remaining": max(0, limit - used),
            }

        active_limit = self._client_active_limit(client)
        active_used = self.store.active_count(client_id=client)
        return {
            "client_id": client,
            "usage_semantics": "gateway_reservations_not_provider_billing",
            "day_utc": datetime.fromtimestamp(since, tz=timezone.utc).date().isoformat(),
            "resets_at": resets_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "jobs": counter(),
            "image": counter("image"),
            "video": counter("video"),
            "active": {
                "used": active_used,
                "limit": active_limit,
                "remaining": max(0, active_limit - active_used),
            },
        }

    def snapshot(self, country_code: str = "") -> dict[str, Any]:
        return dict(provider_snapshot(self._effective_country(country_code)))
