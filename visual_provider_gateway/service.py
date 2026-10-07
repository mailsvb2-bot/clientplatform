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

from .engine import VisualCreativeEngine, build_provider, provider_order, provider_snapshot
from .models import CreativeBrief, CreativeJob
from .providers import GigaChatImageProvider, ProviderTransportError
from .store import JobStore, StoredJob



_SEMANTIC_QA_FLAGS = frozenset(
    {
        "transformation",
        "object_replacement",
        "sequence",
        "listening",
        "watching",
        "reading",
        "using",
        "holding",
        "eating_or_drinking",
        "generic_action",
        "comparison",
        "explicit_text",
        "portrait",
        "visible_state",
        "storyboard",
        "presentation_change",
        "presentation_transition",
    }
)

_SAFE_EXPLICIT_RETRY_ERRORS = frozenset(
    {
        "no_visual_provider_available",
        "visual_creative_disabled",
        "visual_provider_submit_invalid_request",
        "visual_provider_submit_http_400",
        "visual_provider_submit_http_401",
        "visual_provider_submit_http_403",
        "visual_provider_submit_http_404",
        "visual_provider_submit_http_410",
        "visual_provider_submit_http_422",
        "visual_provider_submit_connect_unreachable",
    }
)

class VisualGatewayService:
    def __init__(self, *, store: JobStore | None = None, engine: VisualCreativeEngine | None = None) -> None:
        self.store = store or JobStore()
        self.engine = engine or VisualCreativeEngine()

    @staticmethod
    def _provider_state_json(
        value: object,
        *,
        country_code: str = "",
    ) -> str:
        provider_state = value if isinstance(value, dict) else {}
        country = re.sub(
            r"[^A-Z0-9]",
            "",
            str(country_code or "").strip().upper(),
        )
        if not provider_state and not country:
            return ""
        encoded_value: dict[str, object]
        if country:
            encoded_value = {
                "_gateway": {"country_code": country},
                "provider": dict(provider_state),
            }
        else:
            encoded_value = dict(provider_state)
        encoded = json.dumps(
            encoded_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > 8192:
            raise ValueError("visual_provider_state_too_large")
        return encoded

    @staticmethod
    def _decoded_provider_state(value: object) -> dict[str, Any]:
        raw = str(value or "").strip()
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_visual_provider_state") from exc
        if not isinstance(decoded, dict):
            raise ValueError("invalid_visual_provider_state")
        return decoded

    @classmethod
    def _provider_state(cls, value: object) -> dict[str, Any]:
        decoded = cls._decoded_provider_state(value)
        provider = decoded.get("provider")
        gateway = decoded.get("_gateway")
        if isinstance(gateway, dict) and isinstance(provider, dict):
            return dict(provider)
        return decoded

    @classmethod
    def _provider_policy_country(cls, value: object) -> str:
        decoded = cls._decoded_provider_state(value)
        gateway = decoded.get("_gateway")
        if not isinstance(gateway, dict):
            return ""
        return re.sub(
            r"[^A-Z0-9]",
            "",
            str(gateway.get("country_code") or "").strip().upper(),
        )

    @staticmethod
    def _output_root() -> Path:
        return Path(
            os.getenv("VISUAL_CREATIVE_OUTPUT_DIR", "data/visual_creatives")
        ).expanduser().resolve()

    def _asset_ready(self, job: StoredJob) -> bool:
        if job.status != "succeeded" or not job.asset_path:
            return False
        try:
            root = self._output_root()
            candidate = Path(job.asset_path).expanduser().resolve()
            candidate.relative_to(root)
            return candidate.is_file()
        except (OSError, ValueError):
            return False

    def _cleanup_transient_assets(self) -> int:
        """Bound transient provider media without retaining user bytes indefinitely."""

        root = self._output_root()
        ttl = self._env_int(
            "VISUAL_TRANSIENT_ASSET_TTL_SECONDS",
            21_600,
            minimum=300,
            maximum=86_400,
        )
        limit = self._env_int(
            "VISUAL_TRANSIENT_ASSET_CLEANUP_LIMIT",
            200,
            minimum=1,
            maximum=5_000,
        )
        try:
            if not root.is_dir():
                return 0
        except OSError:
            return 0
        cutoff = time.time() - ttl
        removed = 0
        try:
            candidates = sorted(
                (item for item in root.iterdir() if item.is_file()),
                key=lambda item: item.stat().st_mtime,
            )
        except OSError:
            return 0
        for candidate in candidates:
            if removed >= limit:
                break
            try:
                if candidate.stat().st_mtime > cutoff:
                    continue
                candidate.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        return removed

    def _response(self, job: StoredJob) -> dict[str, Any]:
        return {
            "id": job.id,
            "provider": job.provider,
            "scope_id": job.scope_id,
            "kind": job.kind,
            "status": job.status,
            "model": job.model,
            "mime_type": job.mime_type,
            "error_code": job.error_code,
            "asset_ready": self._asset_ready(job),
        }

    @staticmethod
    def _scene_contract(value: object) -> dict[str, object] | None:
        if value is None:
            return None
        required = {
            "version",
            "topology",
            "primary_subject",
            "initial_state",
            "actions",
            "cause",
            "transition",
            "final_state",
            "explicit_text",
            "required_evidence",
            "forbidden",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("visual_scene_contract_invalid")
        topology = str(value.get("topology") or "").strip().lower()
        if value.get("version") != 1 or topology not in {
            "static",
            "action",
            "transformation",
            "sequence",
            "comparison",
            "replacement",
        }:
            raise ValueError("visual_scene_contract_invalid")

        def text(raw: object, *, limit: int) -> str:
            token = " ".join(str(raw or "").replace("\x00", " ").split()).strip()
            if len(token) > limit or any(ord(char) < 32 for char in token):
                raise ValueError("visual_scene_contract_invalid")
            return token

        def items(raw: object) -> list[str]:
            if not isinstance(raw, list) or len(raw) > 8:
                raise ValueError("visual_scene_contract_invalid")
            out: list[str] = []
            for item in raw:
                token = text(item, limit=240)
                if token and token not in out:
                    out.append(token)
            return out

        return {
            "version": 1,
            "topology": topology,
            "primary_subject": text(value.get("primary_subject"), limit=160),
            "initial_state": items(value.get("initial_state")),
            "actions": items(value.get("actions")),
            "cause": text(value.get("cause"), limit=240),
            "transition": items(value.get("transition")),
            "final_state": items(value.get("final_state")),
            "explicit_text": items(value.get("explicit_text")),
            "required_evidence": items(value.get("required_evidence")),
            "forbidden": items(value.get("forbidden")),
        }

    @staticmethod
    def _semantic_qa_contract(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("visual_semantic_qa_contract_invalid")
        version = value.get("version")
        expected = {
            "version",
            "kind",
            "country_code",
            "owner_request",
            "semantic_flags",
        }
        if version == 2:
            expected.add("scene_contract")
        if version not in {1, 2} or set(value) != expected:
            raise ValueError("visual_semantic_qa_contract_invalid")
        request = " ".join(str(value.get("owner_request") or "").split()).strip()
        country_code = str(value.get("country_code") or "").strip().upper()
        raw_flags = value.get("semantic_flags")
        if (
            str(value.get("kind") or "").strip().lower() != "image"
            or len(country_code) > 16
            or any(
                not (char.isalnum() or char in {"-", "_"})
                for char in country_code
            )
            or not request
            or len(request) > 1500
            or any(ord(char) < 32 for char in request)
            or not isinstance(raw_flags, list)
            or len(raw_flags) > len(_SEMANTIC_QA_FLAGS)
        ):
            raise ValueError("visual_semantic_qa_contract_invalid")
        flags = tuple(str(item or "").strip() for item in raw_flags)
        if (
            len(set(flags)) != len(flags)
            or any(not item or item not in _SEMANTIC_QA_FLAGS for item in flags)
        ):
            raise ValueError("visual_semantic_qa_contract_invalid")
        scene_contract = None
        if version == 2:
            scene_contract = VisualGatewayService._scene_contract(
                value.get("scene_contract")
            )
            if scene_contract is None:
                raise ValueError("visual_semantic_qa_contract_invalid")
        result: dict[str, object] = {
            "version": int(version),
            "kind": "image",
            "country_code": country_code,
            "owner_request": request,
            "semantic_flags": list(flags),
        }
        if version == 2:
            result["scene_contract"] = scene_contract
        return result

    @staticmethod
    def _semantic_qa_digest(contract: dict[str, object]) -> str:
        return hashlib.sha256(
            json.dumps(
                contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _semantic_qa_public(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {"status": "unavailable", "issues": [], "summary": ""}
        status = str(value.get("status") or "").strip().lower()
        if status == "running":
            return {"status": "unavailable", "issues": [], "summary": ""}
        if status not in {"pass", "needs_review", "unavailable"}:
            return {"status": "unavailable", "issues": [], "summary": ""}
        raw_issues = value.get("issues")
        issues = (
            [
                " ".join(str(item or "").split()).strip()[:180]
                for item in raw_issues[:5]
                if " ".join(str(item or "").split()).strip()
            ]
            if isinstance(raw_issues, list)
            else []
        )
        if status != "needs_review":
            issues = []
        summary = " ".join(str(value.get("summary") or "").split()).strip()[:500]
        return {"status": status, "issues": issues, "summary": summary}

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
        self._cleanup_transient_assets()
        scope_id = str(payload.get("scope_id") or "global").strip() or "global"
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        kind = str(payload.get("kind") or "image").strip().lower()
        effective_country = self._effective_country(payload.get("country_code"))

        fingerprint_payload = {
            key: payload.get(key)
            for key in (
                "kind", "prompt", "preferred_provider", "aspect_ratio",
                "duration_seconds", "negative_prompt", "reference_url", "brand_context",
                "seed", "scene_contract", "scope_id"
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
                scene_contract=self._scene_contract(payload.get("scene_contract")),
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
            provider_state_json=self._provider_state_json(
                job.provider_payload,
                country_code=effective_country,
            ),
        )
        return self._response(stored)

    def poll(self, gateway_id: str, *, client_id: str, scope_id: str) -> dict[str, Any]:
        self._cleanup_transient_assets()
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
                    # Provider identity is still unknown after the reservation timeout.
                    # The process may have died after the external provider accepted
                    # the paid request but before its job id was persisted. Treat this
                    # as ambiguous, never as a safe pre-acceptance failure.
                    error_code="visual_gateway_submit_ambiguous",
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
                provider_payload=self._provider_state(stored.provider_state_json),
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
            provider_state_json=self._provider_state_json(
                refreshed.provider_payload,
                country_code=self._provider_policy_country(
                    stored.provider_state_json
                ),
            ),
        )
        return self._response(updated)

    def content_path(self, gateway_id: str, *, client_id: str, scope_id: str) -> tuple[Path, str]:
        self._cleanup_transient_assets()
        stored = self.store.get(gateway_id, client_id=client_id, scope_id=scope_id)
        if stored.status != "succeeded" or not stored.asset_path:
            raise FileNotFoundError(gateway_id)
        output_root = self._output_root()
        candidate = Path(stored.asset_path).expanduser().resolve()
        try:
            candidate.relative_to(output_root)
        except ValueError as exc:
            raise FileNotFoundError(gateway_id) from exc
        if not candidate.is_file():
            raise FileNotFoundError(gateway_id)
        return candidate, stored.mime_type or "application/octet-stream"

    def semantic_qa(
        self,
        gateway_id: str,
        *,
        client_id: str,
        scope_id: str,
        contract: object,
    ) -> dict[str, object]:
        """Run at most one advisory vision review for a succeeded image job.

        The durable claim happens before provider I/O. A retry, redelivery or crash
        can therefore never trigger a second vision review for the same contract.
        The review is advisory only and cannot change the image job status.
        """

        normalized = self._semantic_qa_contract(contract)
        digest = self._semantic_qa_digest(normalized)
        stored = self.store.get(
            gateway_id,
            client_id=client_id,
            scope_id=scope_id,
        )
        if stored.kind != "image" or stored.status != "succeeded":
            raise ValueError("visual_semantic_qa_job_not_reviewable")
        if not self._asset_ready(stored):
            return {"status": "unavailable", "issues": [], "summary": ""}

        existing, created = self.store.claim_semantic_qa(
            stored.id,
            client_id=client_id,
            scope_id=scope_id,
            contract_digest=digest,
        )
        if not created:
            return self._semantic_qa_public(existing)

        unavailable: dict[str, object] = {
            "status": "unavailable",
            "issues": [],
            "summary": "",
        }
        if not self._truthy_env("VISUAL_SEMANTIC_QA_ENABLED", "1"):
            completed = self.store.complete_semantic_qa(
                stored.id,
                client_id=client_id,
                scope_id=scope_id,
                contract_digest=digest,
                result=unavailable,
            )
            return self._semantic_qa_public(completed)

        try:
            job_country = self._provider_policy_country(stored.provider_state_json)
            requested_country = re.sub(
                r"[^A-Z0-9]",
                "",
                str(normalized.get("country_code") or "").strip().upper(),
            )
            if not job_country or requested_country != job_country:
                result = unavailable
            else:
                allowed = provider_order(
                    "image",
                    country_code=job_country,
                )
                if "gigachat" not in allowed:
                    result = unavailable
                else:
                    provider = build_provider("gigachat")
                    if (
                        not isinstance(provider, GigaChatImageProvider)
                        or not provider.configured("image")
                    ):
                        result = unavailable
                    else:
                        root = self._output_root()
                        candidate = Path(stored.asset_path).expanduser().resolve()
                        candidate.relative_to(root)
                        raw_flags = normalized.get("semantic_flags")
                        if not isinstance(raw_flags, list):
                            raise ValueError("visual_semantic_qa_contract_invalid")
                        result = provider.review_image_semantics(
                            image_path=candidate,
                            owner_request=str(normalized["owner_request"]),
                            semantic_flags=tuple(str(item) for item in raw_flags),
                            scene_contract=(
                                normalized.get("scene_contract")
                                if isinstance(normalized.get("scene_contract"), dict)
                                else None
                            ),
                        )
        except (
            ProviderTransportError,
            OSError,
            TypeError,
            ValueError,
        ):
            result = unavailable

        public = self._semantic_qa_public(result)
        completed = self.store.complete_semantic_qa(
            stored.id,
            client_id=client_id,
            scope_id=scope_id,
            contract_digest=digest,
            result=public,
        )
        return self._semantic_qa_public(completed)

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
        payload = dict(provider_snapshot(self._effective_country(country_code)))
        runtime_snapshot = getattr(self.engine, "runtime_snapshot", None)
        runtime = (
            runtime_snapshot()
            if callable(runtime_snapshot)
            else {
                "image": {},
                "video": {},
                "circuits_open_seconds": {},
            }
        )
        payload["runtime"] = runtime

        circuits = runtime.get("circuits_open_seconds") if isinstance(runtime, dict) else {}
        open_providers = {
            str(name)
            for name, seconds in (circuits.items() if isinstance(circuits, dict) else ())
            if int(seconds or 0) > 0
        }
        if "yandexart" in open_providers:
            open_providers.add("yandexart_motion")
        if open_providers:
            for field in (
                "configured_image",
                "configured_video",
                "configured_video_native",
                "configured_video_motion",
            ):
                current = payload.get(field)
                if isinstance(current, (list, tuple)):
                    payload[field] = tuple(
                        str(name) for name in current if str(name) not in open_providers
                    )

            native = tuple(payload.get("configured_video_native") or ())
            motion = tuple(payload.get("configured_video_motion") or ())
            payload["video_generation_mode"] = (
                "native" if native else "motion" if motion else "unavailable"
            )
        return payload
