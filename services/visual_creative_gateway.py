from __future__ import annotations

import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class VisualCreativeGatewayError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VisualCreativeJob:
    id: str
    provider: str
    scope_id: str
    kind: str
    status: str
    model: str = ""
    mime_type: str = ""
    error_code: str = ""
    asset_ready: bool = False

    @property
    def done(self) -> bool:
        return self.status in {"succeeded", "failed"}


@dataclass(frozen=True, slots=True)
class VisualCreativeBrief:
    kind: str
    prompt: str
    country_code: str = ""
    preferred_provider: str = ""
    aspect_ratio: str = "1:1"
    duration_seconds: int = 5
    negative_prompt: str = ""
    reference_url: str = ""
    brand_context: str = ""
    seed: int | None = None


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _base_url() -> str:
    value = str(os.getenv("VISUAL_GATEWAY_URL", "") or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise VisualCreativeGatewayError("visual_gateway_not_configured")
    port = f":{parsed.port}" if parsed.port else ""
    prefix = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.hostname}{port}{prefix}"


def _headers(*, json_body: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = str(os.getenv("VISUAL_GATEWAY_TOKEN", "") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _read_limited(response: Any, limit: int) -> bytes:
    content_length = str(response.headers.get("Content-Length") or "").strip()
    if content_length:
        try:
            if int(content_length) > limit:
                raise VisualCreativeGatewayError("visual_gateway_response_too_large")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(1024 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise VisualCreativeGatewayError("visual_gateway_response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    max_bytes: int,
    timeout_seconds: int | None = None,
) -> tuple[dict[str, str], bytes]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _base_url() + path,
        data=body,
        method=str(method).upper(),
        headers=_headers(json_body=payload is not None),
    )
    configured_timeout = _env_int(
        "VISUAL_GATEWAY_TIMEOUT_SECONDS",
        30,
        minimum=3,
        maximum=300,
    )
    timeout = (
        configured_timeout
        if timeout_seconds is None
        else max(configured_timeout, min(int(timeout_seconds), 300))
    )
    try:
        with urllib.request.urlopen(  # nosec B310 - operator-configured gateway URL
            request,
            timeout=timeout,
        ) as response:
            raw = _read_limited(response, max_bytes)
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            return headers, raw
    except urllib.error.HTTPError as exc:
        try:
            exc.read(65536)
        except OSError:
            pass
        raise VisualCreativeGatewayError(
            f"visual_gateway_http_{int(exc.code)}"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise VisualCreativeGatewayError(
            f"visual_gateway_transport_{type(exc).__name__}"
        ) from None


def _json(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    _, raw = _request(
        method,
        path,
        payload=payload,
        max_bytes=_env_int(
            "VISUAL_GATEWAY_MAX_JSON_BYTES",
            1024 * 1024,
            minimum=65536,
            maximum=8 * 1024 * 1024,
        ),
        timeout_seconds=timeout_seconds,
    )
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualCreativeGatewayError("visual_gateway_invalid_json") from exc
    if not isinstance(value, dict):
        raise VisualCreativeGatewayError("visual_gateway_invalid_response")
    return value


def _job(value: dict[str, Any]) -> VisualCreativeJob:
    job_id = str(value.get("id") or "").strip()
    status = str(value.get("status") or "failed").strip().lower()
    kind = str(value.get("kind") or "").strip().lower()
    scope_id = str(value.get("scope_id") or "").strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id)
        or not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", scope_id)
        or kind not in {"image", "video"}
        or status not in {"queued", "running", "succeeded", "failed"}
    ):
        raise VisualCreativeGatewayError("visual_gateway_invalid_job")
    return VisualCreativeJob(
        id=job_id,
        provider=str(value.get("provider") or ""),
        scope_id=scope_id,
        kind=kind,
        status=status,
        model=str(value.get("model") or ""),
        mime_type=str(value.get("mime_type") or ""),
        error_code=str(value.get("error_code") or ""),
        asset_ready=bool(value.get("asset_ready")),
    )


def submit_visual(
    brief: VisualCreativeBrief,
    *,
    scope_id: str,
    idempotency_key: str,
    wait_seconds: int = 0,
) -> VisualCreativeJob:
    kind = str(brief.kind or "").strip().lower()
    prompt = str(brief.prompt or "").strip()
    if kind not in {"image", "video"} or not prompt:
        raise ValueError("valid visual kind and prompt are required")
    scope = str(scope_id or "").strip()
    idem = str(idempotency_key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", scope) or not re.fullmatch(
        r"[A-Za-z0-9_.:@/-]{8,200}",
        idem,
    ):
        raise ValueError("valid visual scope and idempotency key are required")
    bounded_wait = max(0, min(int(wait_seconds or 0), 60))
    payload = {
        "kind": kind,
        "prompt": prompt,
        "country_code": str(brief.country_code or ""),
        "preferred_provider": str(brief.preferred_provider or ""),
        "aspect_ratio": str(brief.aspect_ratio or "1:1"),
        "duration_seconds": max(2, min(int(brief.duration_seconds or 5), 15)),
        "negative_prompt": str(brief.negative_prompt or ""),
        "reference_url": str(brief.reference_url or ""),
        "brand_context": str(brief.brand_context or ""),
        "wait_seconds": bounded_wait,
        "seed": brief.seed,
        "scope_id": scope,
        "idempotency_key": idem,
    }
    return _job(
        _json(
            "POST",
            "/v1/creative/generations",
            payload=payload,
            timeout_seconds=bounded_wait + 15,
        )
    )


def poll_visual(job_id: str, *, scope_id: str) -> VisualCreativeJob:
    raw = str(job_id or "").strip()
    scope = str(scope_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw):
        raise ValueError("valid visual job id is required")
    if not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", scope):
        raise ValueError("valid visual scope is required")
    token = urllib.parse.quote(raw, safe="")
    query = urllib.parse.urlencode({"scope_id": scope})
    return _job(_json("GET", f"/v1/creative/generations/{token}?{query}"))


def wait_visual(
    job: VisualCreativeJob,
    *,
    wait_seconds: int = 20,
    poll_interval: float = 2.0,
) -> VisualCreativeJob:
    if job.done or wait_seconds <= 0:
        return job
    deadline = time.monotonic() + max(0, min(int(wait_seconds), 60))
    current = job
    while time.monotonic() < deadline and not current.done:
        time.sleep(max(0.2, min(float(poll_interval), 5.0)))
        current = poll_visual(current.id, scope_id=current.scope_id)
    return current


def download_visual(job: VisualCreativeJob, *, output_dir: str | None = None) -> Path:
    if job.status != "succeeded" or not job.asset_ready:
        raise VisualCreativeGatewayError("visual_content_not_ready")
    token = urllib.parse.quote(job.id, safe="")
    if not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", job.scope_id):
        raise VisualCreativeGatewayError("visual_gateway_invalid_scope")
    query = urllib.parse.urlencode({"scope_id": job.scope_id})
    max_media = _env_int(
        "VISUAL_GATEWAY_MAX_MEDIA_BYTES",
        256 * 1024 * 1024,
        minimum=1024 * 1024,
        maximum=1024 * 1024 * 1024,
    )
    headers, raw = _request(
        "GET",
        f"/v1/creative/generations/{token}/content?{query}",
        max_bytes=max_media,
    )
    mime = str(headers.get("content-type") or job.mime_type or "").split(";", 1)[0].strip().lower()
    expected = "video/" if job.kind == "video" else "image/"
    if mime and mime != "application/octet-stream" and not mime.startswith(expected):
        raise VisualCreativeGatewayError("visual_gateway_unexpected_media_type")
    suffix = mimetypes.guess_extension(mime) if mime else None
    if suffix == ".jpe":
        suffix = ".jpg"
    suffix = suffix or (".mp4" if job.kind == "video" else ".jpg")
    root = Path(
        output_dir or os.getenv("VISUAL_CREATIVE_OUTPUT_DIR", "data/visual_creatives")
    ).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{job.kind}-{job.id}{suffix}"
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, target)
    return target


def gateway_snapshot() -> dict[str, Any]:
    base = str(os.getenv("VISUAL_GATEWAY_URL", "") or "").strip()
    parsed = urllib.parse.urlsplit(base) if base else None
    safe_base = ""
    if parsed is not None and parsed.scheme in {"http", "https"} and parsed.hostname:
        port = f":{parsed.port}" if parsed.port else ""
        safe_base = f"{parsed.scheme}://{parsed.hostname}{port}"
    return {
        "configured": bool(safe_base),
        "base_url": safe_base,
        "token_configured": bool(str(os.getenv("VISUAL_GATEWAY_TOKEN", "") or "").strip()),
    }


__all__ = [
    "VisualCreativeBrief",
    "VisualCreativeGatewayError",
    "VisualCreativeJob",
    "download_visual",
    "gateway_snapshot",
    "poll_visual",
    "submit_visual",
    "wait_visual",
]
