from __future__ import annotations

import hashlib
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
    def __init__(
        self,
        code: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(code)
        self.code = str(code)
        self.retry_after_seconds = retry_after_seconds


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
class VisualSemanticQA:
    status: str
    issues: tuple[str, ...] = ()
    summary: str = ""

    @property
    def needs_review(self) -> bool:
        return self.status == "needs_review"


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
    scene_contract: dict[str, object] | None = None

_RENDER_PACK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_RENDER_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9_.:@/-]{8,200}")
_RENDER_SHA_RE = re.compile(r"[0-9a-f]{64}")
_RENDER_FORMATS = frozenset({"square", "feed", "story", "landscape"})
_TRANSIENT_CONTENT_ERRORS = frozenset({
    "visual_gateway_http_404",
    "visual_gateway_http_408",
    "visual_gateway_http_425",
    "visual_gateway_http_429",
    "visual_gateway_http_500",
    "visual_gateway_http_502",
    "visual_gateway_http_503",
    "visual_gateway_http_504",
    "visual_gateway_response_incomplete",
})
_CONTENT_RETRY_BASE_SECONDS = 0.5
_CONTENT_RETRY_CAP_SECONDS = 10.0
_CONTENT_RETRY_AFTER_CAP_SECONDS = 30.0

_RENDER_DIMENSIONS = {"square": (1080, 1080), "feed": (1080, 1350), "story": (1080, 1920), "landscape": (1200, 628)}


@dataclass(frozen=True, slots=True)
class VisualRenderAsset:
    format_id: str
    kind: str
    width: int
    height: int
    mime_type: str
    sha256: str
    asset_ready: bool
    quality: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VisualRenderPack:
    id: str
    scope_id: str
    source_job_id: str
    status: str
    error_code: str
    assets: tuple[VisualRenderAsset, ...]

def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _content_recovery_deadline_seconds(kind: str) -> int:
    normalized = str(kind or "").strip().lower()
    if normalized == "video":
        return _env_int(
            "VISUAL_CONTENT_RECOVERY_VIDEO_SECONDS",
            120,
            minimum=10,
            maximum=600,
        )
    return _env_int(
        "VISUAL_CONTENT_RECOVERY_IMAGE_SECONDS",
        60,
        minimum=5,
        maximum=300,
    )


def _content_transfer_timeout_seconds(kind: str) -> int:
    normalized = str(kind or "").strip().lower()
    if normalized == "video":
        return _env_int(
            "VISUAL_CONTENT_TRANSFER_VIDEO_TIMEOUT_SECONDS",
            180,
            minimum=10,
            maximum=300,
        )
    return _env_int(
        "VISUAL_CONTENT_TRANSFER_IMAGE_TIMEOUT_SECONDS",
        60,
        minimum=5,
        maximum=300,
    )


def _content_retry_delay(
    job_id: str,
    attempt: int,
    *,
    retry_after_seconds: float | None = None,
) -> float:
    exponent = max(0, min(int(attempt), 8))
    base = min(
        _CONTENT_RETRY_BASE_SECONDS * (2 ** exponent),
        _CONTENT_RETRY_CAP_SECONDS,
    )
    digest = hashlib.sha256(
        f"{str(job_id or '')}:{exponent}".encode("utf-8", "replace")
    ).digest()
    jitter = 0.80 + (digest[0] / 255.0) * 0.40
    delay = base * jitter
    if retry_after_seconds is not None:
        try:
            retry_after = float(retry_after_seconds)
        except (TypeError, ValueError):
            retry_after = 0.0
        if 0.0 < retry_after < 1_000_000.0:
            delay = max(delay, min(retry_after, _CONTENT_RETRY_AFTER_CAP_SECONDS))
    return min(delay, _CONTENT_RETRY_AFTER_CAP_SECONDS)


def _content_error_is_transient(exc: VisualCreativeGatewayError) -> bool:
    code = str(exc or "").strip()
    return code in _TRANSIENT_CONTENT_ERRORS or code.startswith(
        "visual_gateway_transport_"
    )


def _retry_after_seconds(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if not (0.0 < seconds < 1_000_000.0):
        return None
    return min(seconds, _CONTENT_RETRY_AFTER_CAP_SECONDS)


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
    try:
        port_value = parsed.port
    except ValueError as exc:
        raise VisualCreativeGatewayError("visual_gateway_not_configured") from exc
    port = f":{port_value}" if port_value else ""
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
    expected_length: int | None = None
    if content_length:
        try:
            parsed_length = int(content_length)
        except ValueError:
            parsed_length = -1
        if parsed_length >= 0:
            expected_length = parsed_length
            if expected_length > limit:
                raise VisualCreativeGatewayError("visual_gateway_response_too_large")
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
    if expected_length is not None and total != expected_length:
        raise VisualCreativeGatewayError("visual_gateway_response_incomplete")
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
        retry_after = _retry_after_seconds(
            exc.headers.get("Retry-After") if exc.headers is not None else None
        )
        try:
            exc.read(65536)
        except OSError:
            pass
        raise VisualCreativeGatewayError(
            f"visual_gateway_http_{int(exc.code)}",
            retry_after_seconds=retry_after,
        ) from None
    except urllib.error.URLError as exc:
        raise VisualCreativeGatewayError(
            f"visual_gateway_transport_{type(exc).__name__}"
        ) from None
    except TimeoutError as exc:
        raise VisualCreativeGatewayError(
            f"visual_gateway_transport_{type(exc).__name__}"
        ) from None
    except OSError as exc:
        raise VisualCreativeGatewayError(
            f"visual_gateway_transport_{type(exc).__name__}"
        ) from None
    except ValueError as exc:
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


def _require_scope(job: VisualCreativeJob, *, expected_scope: str) -> VisualCreativeJob:
    if job.scope_id != expected_scope:
        raise VisualCreativeGatewayError("visual_gateway_scope_mismatch")
    return job


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
        "scene_contract": brief.scene_contract,
        "scope_id": scope,
        "idempotency_key": idem,
    }
    job = _job(
        _json(
            "POST",
            "/v1/creative/generations",
            payload=payload,
            timeout_seconds=bounded_wait + 15,
        )
    )
    return _require_scope(job, expected_scope=scope)


def poll_visual(job_id: str, *, scope_id: str) -> VisualCreativeJob:
    raw = str(job_id or "").strip()
    scope = str(scope_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw):
        raise ValueError("valid visual job id is required")
    if not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", scope):
        raise ValueError("valid visual scope is required")
    token = urllib.parse.quote(raw, safe="")
    query = urllib.parse.urlencode({"scope_id": scope})
    job = _job(_json("GET", f"/v1/creative/generations/{token}?{query}"))
    return _require_scope(job, expected_scope=scope)


def review_visual_semantics(
    job: VisualCreativeJob,
    *,
    contract: dict[str, object],
) -> VisualSemanticQA:
    if (
        job.kind != "image"
        or job.status != "succeeded"
        or not job.asset_ready
    ):
        raise VisualCreativeGatewayError("visual_semantic_qa_job_not_reviewable")
    if not isinstance(contract, dict):
        raise ValueError("valid visual semantic QA contract is required")
    payload = {
        "scope_id": job.scope_id,
        "contract": contract,
    }
    token = urllib.parse.quote(job.id, safe="")
    value = _json(
        "POST",
        f"/v1/creative/generations/{token}/semantic-qa",
        payload=payload,
        timeout_seconds=_env_int(
            "VISUAL_SEMANTIC_QA_TIMEOUT_SECONDS",
            45,
            minimum=5,
            maximum=120,
        ),
    )
    status = str(value.get("status") or "").strip().lower()
    raw_issues = value.get("issues")
    summary = " ".join(str(value.get("summary") or "").split()).strip()
    if (
        status not in {"pass", "needs_review", "unavailable"}
        or not isinstance(raw_issues, list)
        or len(raw_issues) > 5
        or any(not isinstance(item, str) or len(item) > 180 for item in raw_issues)
        or len(summary) > 500
    ):
        raise VisualCreativeGatewayError("visual_semantic_qa_invalid_response")
    issues = tuple(
        " ".join(item.split()).strip()
        for item in raw_issues
        if " ".join(item.split()).strip()
    )
    if status != "needs_review":
        issues = ()
    return VisualSemanticQA(
        status=status,
        issues=issues,
        summary=summary,
    )


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
    content_path = f"/v1/creative/generations/{token}/content?{query}"
    recovery_deadline = (
        time.monotonic() + _content_recovery_deadline_seconds(job.kind)
    )
    transfer_timeout = _content_transfer_timeout_seconds(job.kind)
    attempt = 0
    while True:
        try:
            headers, raw = _request(
                "GET",
                content_path,
                max_bytes=max_media,
                timeout_seconds=transfer_timeout,
            )
            break
        except VisualCreativeGatewayError as exc:
            if not _content_error_is_transient(exc):
                raise
            remaining = recovery_deadline - time.monotonic()
            if remaining <= 0:
                raise
            delay = _content_retry_delay(
                job.id,
                attempt,
                retry_after_seconds=exc.retry_after_seconds,
            )
            if delay >= remaining:
                raise
            time.sleep(delay)
            attempt += 1
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


def _render_pack(
    value: dict[str, Any],
    *,
    expected_scope_id: str,
    expected_source_job_id: str,
    expected_formats: tuple[str, ...] = (),
    expected_kind: str = "",
) -> VisualRenderPack:
    pack_id = str(value.get("id") or "").strip()
    scope_id = str(value.get("scope_id") or "").strip()
    source_job_id = str(value.get("source_job_id") or "").strip()
    status = str(value.get("status") or "failed").strip().lower()
    raw_assets = value.get("assets")
    if (
        _RENDER_PACK_ID_RE.fullmatch(pack_id) is None
        or not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", scope_id)
        or scope_id != expected_scope_id
        or source_job_id != expected_source_job_id
        or status not in {"running", "succeeded", "failed"}
        or not isinstance(raw_assets, list)
        or len(raw_assets) > 4
    ):
        raise VisualCreativeGatewayError("visual_gateway_invalid_render_pack")
    selected = tuple(dict.fromkeys(str(item or "").strip().lower() for item in expected_formats))
    if any(item not in _RENDER_FORMATS for item in selected):
        raise VisualCreativeGatewayError("visual_gateway_invalid_render_pack")
    expected_kind = str(expected_kind or "").strip().lower()
    if expected_kind and expected_kind not in {"image", "video"}:
        raise VisualCreativeGatewayError("visual_gateway_invalid_render_pack")
    assets: list[VisualRenderAsset] = []
    seen: set[str] = set()
    for item in raw_assets:
        if not isinstance(item, dict):
            raise VisualCreativeGatewayError("visual_gateway_invalid_render_asset")
        format_id = str(item.get("format_id") or "").strip().lower()
        kind = str(item.get("kind") or "").strip().lower()
        mime_type = str(item.get("mime_type") or "").strip().lower()
        sha256 = str(item.get("sha256") or "").strip().lower()
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        ready = item.get("asset_ready")
        if (
            format_id not in _RENDER_FORMATS
            or format_id in seen
            or kind not in {"image", "video"}
            or not isinstance(ready, bool)
            or (width, height) != _RENDER_DIMENSIONS[format_id]
            or (expected_kind and kind != expected_kind)
            or not mime_type
            or re.fullmatch(r"[A-Za-z0-9!#$&^_.+/-]{1,128}", mime_type) is None
            or not mime_type.startswith("video/" if kind == "video" else "image/")
            or (status == "succeeded" and (_RENDER_SHA_RE.fullmatch(sha256) is None or ready is not True))
            or (sha256 and _RENDER_SHA_RE.fullmatch(sha256) is None)
        ):
            raise VisualCreativeGatewayError("visual_gateway_invalid_render_asset")
        seen.add(format_id)
        assets.append(
            VisualRenderAsset(
                format_id=format_id,
                kind=kind,
                width=width,
                height=height,
                mime_type=mime_type,
                sha256=sha256,
                asset_ready=ready,
                quality=dict(item.get("quality") or {}) if isinstance(item.get("quality"), dict) else {},
            )
        )
    if status == "succeeded":
        if not assets or (selected and set(seen) != set(selected)):
            raise VisualCreativeGatewayError("visual_gateway_incomplete_render_pack")
    elif selected and any(item not in set(selected) for item in seen):
        raise VisualCreativeGatewayError("visual_gateway_unexpected_render_format")
    return VisualRenderPack(
        id=pack_id,
        scope_id=scope_id,
        source_job_id=source_job_id,
        status=status,
        error_code=str(value.get("error_code") or "")[:160],
        assets=tuple(assets),
    )


def render_visual_pack(
    job: VisualCreativeJob,
    *,
    formats: tuple[str, ...],
    composition: dict[str, Any],
    idempotency_key: str,
) -> VisualRenderPack:
    if job.status != "succeeded" or not job.asset_ready:
        raise VisualCreativeGatewayError("visual_source_not_ready")
    selected: list[str] = []
    for raw in formats:
        token = str(raw or "").strip().lower()
        if token not in _RENDER_FORMATS:
            raise ValueError("invalid_visual_render_format")
        if token not in selected:
            selected.append(token)
    if not selected or len(selected) > 4:
        raise ValueError("visual_render_formats_required")
    idem = str(idempotency_key or "").strip()
    if _RENDER_IDEMPOTENCY_RE.fullmatch(idem) is None:
        raise ValueError("visual_render_idempotency_key_invalid")
    if not isinstance(composition, dict):
        raise ValueError("visual_render_composition_required")
    value = _json(
        "POST",
        "/v1/creative/render-packs",
        payload={
            "source_job_id": job.id,
            "scope_id": job.scope_id,
            "idempotency_key": idem,
            "formats": selected,
            "composition": dict(composition),
        },
        timeout_seconds=300,
    )
    return _render_pack(
        value,
        expected_scope_id=job.scope_id,
        expected_source_job_id=job.id,
        expected_formats=tuple(selected),
        expected_kind=job.kind,
    )


def download_render_asset(
    pack: VisualRenderPack,
    format_id: str,
    *,
    output_dir: str | None = None,
) -> Path:
    token = str(format_id or "").strip().lower()
    asset = next((item for item in pack.assets if item.format_id == token), None)
    if pack.status != "succeeded" or asset is None or not asset.asset_ready:
        raise VisualCreativeGatewayError("visual_render_content_not_ready")
    if _RENDER_PACK_ID_RE.fullmatch(pack.id) is None or token not in _RENDER_FORMATS:
        raise VisualCreativeGatewayError("visual_gateway_invalid_render_asset")
    query = urllib.parse.urlencode({"scope_id": pack.scope_id})
    headers, raw = _request(
        "GET",
        f"/v1/creative/render-packs/{urllib.parse.quote(pack.id, safe='')}/content/{urllib.parse.quote(token, safe='')}?{query}",
        max_bytes=_env_int(
            "VISUAL_GATEWAY_MAX_MEDIA_BYTES",
            256 * 1024 * 1024,
            minimum=1024 * 1024,
            maximum=1024 * 1024 * 1024,
        ),
    )
    mime = str(headers.get("content-type") or asset.mime_type or "").split(";", 1)[0].strip().lower()
    expected = "video/" if asset.kind == "video" else "image/"
    if mime and mime != "application/octet-stream" and not mime.startswith(expected):
        raise VisualCreativeGatewayError("visual_gateway_unexpected_render_media_type")
    if asset.sha256 and hashlib.sha256(raw).hexdigest() != asset.sha256:
        raise VisualCreativeGatewayError("visual_gateway_render_digest_mismatch")
    suffix = mimetypes.guess_extension(mime) if mime else None
    if suffix == ".jpe":
        suffix = ".jpg"
    suffix = suffix or (".mp4" if asset.kind == "video" else ".jpg")
    root = Path(output_dir or os.getenv("VISUAL_CREATIVE_OUTPUT_DIR", "data/visual_creatives")).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"render-{pack.id}-{token}{suffix}"
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_bytes(raw)
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise VisualCreativeGatewayError("visual_gateway_render_materialization_failed") from exc
    return target


def configured_visual_providers(
    kind: str,
    *,
    country_code: str = "",
) -> tuple[str, ...]:
    """Read provider readiness through the authenticated gateway boundary.

    The response exposes only provider names and never provider credentials.
    This lets owner-facing flows fail before paid consent when deployment
    configuration cannot actually generate the requested media kind.
    """

    visual_kind = str(kind or "").strip().lower()
    if visual_kind not in {"image", "video"}:
        raise ValueError("visual kind must be image or video")
    query = urllib.parse.urlencode({"country_code": str(country_code or "").strip()})
    value = _json("GET", f"/v1/providers?{query}")
    if not bool(value.get("enabled")):
        return ()
    raw = value.get(f"configured_{visual_kind}")
    if not isinstance(raw, (list, tuple)):
        raise VisualCreativeGatewayError("visual_gateway_invalid_provider_snapshot")
    result: list[str] = []
    for item in raw:
        name = str(item or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise VisualCreativeGatewayError("visual_gateway_invalid_provider_snapshot")
        if name not in result:
            result.append(name)
    return tuple(result)


def configured_visual_video_mode(*, country_code: str = "") -> str:
    """Return native/motion/unavailable without exposing provider internals."""

    query = urllib.parse.urlencode({"country_code": str(country_code or "").strip()})
    value = _json("GET", f"/v1/providers?{query}")
    if not bool(value.get("enabled")):
        return "unavailable"
    mode = str(value.get("video_generation_mode") or "").strip().lower()
    if mode in {"native", "motion", "unavailable"}:
        return mode

    # Backward-compatible fallback for an older upstream during rolling deploys.
    raw = value.get("configured_video")
    if not isinstance(raw, (list, tuple)):
        raise VisualCreativeGatewayError("visual_gateway_invalid_provider_snapshot")
    names = tuple(str(item or "").strip().lower() for item in raw)
    if any(name and name != "yandexart_motion" for name in names):
        return "native"
    if "yandexart_motion" in names:
        return "motion"
    return "unavailable"


def gateway_snapshot() -> dict[str, Any]:
    base = str(os.getenv("VISUAL_GATEWAY_URL", "") or "").strip()
    parsed = urllib.parse.urlsplit(base) if base else None
    safe_base = ""
    if parsed is not None and parsed.scheme in {"http", "https"} and parsed.hostname:
        try:
            port_value = parsed.port
        except ValueError:
            pass
        else:
            port = f":{port_value}" if port_value else ""
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
    "VisualRenderAsset",
    "VisualRenderPack",
    "download_render_asset",
    "configured_visual_providers",
    "configured_visual_video_mode",
    "download_visual",
    "gateway_snapshot",
    "poll_visual",
    "render_visual_pack",
    "submit_visual",
    "wait_visual",
]