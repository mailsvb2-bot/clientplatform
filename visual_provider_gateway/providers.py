from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import mimetypes
import os
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Protocol

from .models import CreativeBrief, CreativeJob, ProviderConfig, ensure_output_dir


class CreativeProvider(Protocol):
    config: ProviderConfig

    def supports(self, kind: str) -> bool: ...
    def configured(self, kind: str) -> bool: ...
    def submit(self, brief: CreativeBrief) -> CreativeJob: ...
    def poll(self, job: CreativeJob) -> CreativeJob: ...


class ProviderTransportError(RuntimeError):
    pass


def _ssl_context(ca_bundle_file: str = "") -> ssl.SSLContext:
    bundle = str(ca_bundle_file or "").strip()
    return ssl.create_default_context(cafile=bundle or None)


def _read_limited(stream: Any, max_bytes: int) -> bytes:
    limit = max(1, int(max_bytes))
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(1024 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ProviderTransportError("response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 30,
    max_bytes: int = 4 * 1024 * 1024,
    ca_bundle_file: str = "",
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        url,
        data=body,
        method=method.upper(),
        headers={str(k): str(v) for k, v in (headers or {}).items()},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context(ca_bundle_file)) as response:
            return (
                int(getattr(response, "status", 200) or 200),
                {str(k).lower(): str(v) for k, v in response.headers.items()},
                _read_limited(response, max_bytes),
            )
    except urllib.error.HTTPError as exc:
        try:
            _read_limited(exc, 64 * 1024)
        except (OSError, ProviderTransportError):
            pass
        raise ProviderTransportError(f"http_{getattr(exc, 'code', 0)}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderTransportError(type(exc).__name__) from None


def _json_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
    max_bytes: int = 4 * 1024 * 1024,
    ca_bundle_file: str = "",
) -> dict[str, Any]:
    merged = {"Accept": "application/json", **(headers or {})}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged.setdefault("Content-Type", "application/json")
    _, _, raw = _request(method, url, headers=merged, body=body, timeout=timeout, max_bytes=max_bytes, ca_bundle_file=ca_bundle_file)
    if not raw:
        return {}
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderTransportError("invalid_json_response") from exc
    if not isinstance(decoded, dict):
        raise ProviderTransportError("invalid_object_response")
    return decoded


def _ratio_pair(ratio: str) -> tuple[int, int]:
    left, right = str(ratio or "1:1").split(":", 1)
    return max(1, int(left)), max(1, int(right))


def _suffix_for_mime(mime_type: str, kind: str) -> str:
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    guessed = mimetypes.guess_extension(mime) if mime else None
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    return ".mp4" if kind == "video" else ".jpg"


def _store_asset(config: ProviderConfig, job: CreativeJob, data: bytes) -> CreativeJob:
    if not data:
        return job
    if len(data) > config.max_media_bytes:
        raise ProviderTransportError("media_too_large")
    root = ensure_output_dir(config.output_dir)
    suffix = _suffix_for_mime(job.mime_type, job.kind)
    token = re.sub(r"[^a-zA-Z0-9_.-]+", "-", job.external_id or uuid.uuid4().hex)[:80]
    target = root / f"{job.kind}-{job.provider}-{token}{suffix}"
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    job.asset_path = str(target)
    return job


def _validate_media_url(config: ProviderConfig, url: str) -> None:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderTransportError("invalid_media_url")
    base = urllib.parse.urlparse(config.base_url)
    same_selfhost = (
        config.name == "selfhosted"
        and bool(base.hostname)
        and parsed.hostname.casefold() == str(base.hostname).casefold()
    )
    if parsed.scheme != "https" and not same_selfhost and os.getenv("VISUAL_ALLOW_INSECURE_MEDIA_URLS", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        raise ProviderTransportError("insecure_media_url")
    host = parsed.hostname.casefold()
    if host in {"localhost", "localhost.localdomain"} and not same_selfhost:
        raise ProviderTransportError("private_media_url")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved) and not same_selfhost:
        raise ProviderTransportError("private_media_url")
    if ip is None and not same_selfhost:
        try:
            resolved = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise ProviderTransportError("media_host_unresolvable") from exc
        for address in resolved:
            try:
                resolved_ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if resolved_ip.is_private or resolved_ip.is_loopback or resolved_ip.is_link_local or resolved_ip.is_multicast or resolved_ip.is_reserved:
                raise ProviderTransportError("private_media_url")


def _download_asset(config: ProviderConfig, job: CreativeJob, url: str) -> CreativeJob:
    _validate_media_url(config, url)
    _, headers, raw = _request(
        "GET",
        url,
        timeout=config.timeout_seconds,
        max_bytes=config.max_media_bytes,
    )
    mime = str(headers.get("content-type", "") or "").split(";", 1)[0].strip().lower()
    if mime and mime != "application/octet-stream":
        expected = "video/" if job.kind == "video" else "image/"
        if not mime.startswith(expected):
            raise ProviderTransportError("unexpected_media_type")
    job.mime_type = job.mime_type or mime
    return _store_asset(config, job, raw)


class YandexArtProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def supports(self, kind: str) -> bool:
        return kind == "image"

    def configured(self, kind: str) -> bool:
        return self.supports(kind) and bool(self.config.api_key and (self.config.folder_id or self.config.model_image))

    def _authorization(self) -> str:
        scheme = str(os.getenv("YANDEX_ART_AUTH_SCHEME", "") or "").strip()
        if not scheme:
            scheme = "Bearer" if str(os.getenv("YANDEX_ART_IAM_TOKEN", "") or "").strip() else "Api-Key"
        return f"{scheme} {self.config.api_key}"

    def submit(self, brief: CreativeBrief) -> CreativeJob:
        if not self.configured(brief.kind):
            raise ProviderTransportError("provider_not_configured")
        width, height = _ratio_pair(brief.aspect_ratio)
        model_uri = self.config.model_image or f"art://{self.config.folder_id}/yandex-art/latest"
        payload: dict[str, Any] = {
            "modelUri": model_uri,
            "generationOptions": {
                "aspectRatio": {"widthRatio": str(width), "heightRatio": str(height)},
            },
            "messages": [{"text": brief.prompt}],
        }
        if brief.seed is not None:
            payload["generationOptions"]["seed"] = int(brief.seed)
        data = _json_request(
            "POST",
            self.config.base_url.rstrip("/") + "/foundationModels/v1/imageGenerationAsync",
            headers={"Authorization": self._authorization()},
            payload=payload,
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_json_bytes,
        )
        operation_id = str(data.get("id") or "").strip()
        if not operation_id:
            raise ProviderTransportError("missing_operation_id")
        return CreativeJob(
            provider="yandexart",
            kind="image",
            status="queued",
            external_id=operation_id,
            model=model_uri,
            provider_payload={"operation_id": operation_id},
        )

    def poll(self, job: CreativeJob) -> CreativeJob:
        data = _json_request(
            "GET",
            "https://operation.api.cloud.yandex.net:443/operations/" + urllib.parse.quote(job.external_id),
            headers={"Authorization": self._authorization()},
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_json_bytes,
        )
        if data.get("done") is not True:
            job.status = "running"
            return job
        error = data.get("error")
        if error:
            job.status = "failed"
            job.error_code = "provider_error"
            return job
        response = data.get("response") if isinstance(data.get("response"), dict) else {}
        encoded = str(response.get("image") or "")
        if not encoded:
            job.status = "failed"
            job.error_code = "missing_image"
            return job
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError, TypeError):
            job.status = "failed"
            job.error_code = "invalid_image_encoding"
            return job
        job.status = "succeeded"
        job.mime_type = "image/jpeg"
        return _store_asset(self.config, job, raw)


def _motion_dimensions(ratio: str) -> tuple[int, int]:
    normalized = str(ratio or "1:1").strip()
    return {
        "16:9": (1280, 720),
        "9:16": (720, 1280),
        "4:5": (864, 1080),
        "1:1": (1080, 1080),
    }.get(normalized, (1080, 1080))


def _render_motion_video(
    config: ProviderConfig,
    *,
    image_path: str,
    operation_id: str,
    duration_seconds: int,
    aspect_ratio: str,
) -> str:
    source = Path(image_path)
    if not source.is_file():
        raise ProviderTransportError("motion_source_missing")
    root = ensure_output_dir(config.output_dir)
    width, height = _motion_dimensions(aspect_ratio)
    duration = max(2, min(int(duration_seconds or 5), 15))
    frames = duration * 25
    token = re.sub(r"[^a-zA-Z0-9_.-]+", "-", operation_id or uuid.uuid4().hex)[:80]
    target = root / f"video-yandexart-motion-{token}.mp4"
    temporary = target.with_suffix(".mp4.tmp")
    filter_graph = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"zoompan=z='min(zoom+0.0012,1.08)':d={frames}:s={width}x{height}:fps=25,"
        "format=yuv420p"
    )
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-i",
        str(source),
        "-vf",
        filter_graph,
        "-frames:v",
        str(frames),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temporary),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=max(30, duration * 12),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        temporary.unlink(missing_ok=True)
        raise ProviderTransportError("motion_render_failed") from exc
    if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
        temporary.unlink(missing_ok=True)
        raise ProviderTransportError("motion_render_failed")
    os.replace(temporary, target)
    return str(target)


class YandexArtMotionVideoProvider(YandexArtProvider):
    """Create a short motion MP4 from a YandexART advertising keyframe."""

    def supports(self, kind: str) -> bool:
        return kind == "video"

    def configured(self, kind: str) -> bool:
        return self.supports(kind) and bool(
            self.config.api_key and (self.config.folder_id or self.config.model_image)
        )

    def submit(self, brief: CreativeBrief) -> CreativeJob:
        if not self.configured(brief.kind):
            raise ProviderTransportError("provider_not_configured")
        image_provider = YandexArtProvider(self.config)
        image_job = image_provider.submit(
            CreativeBrief(
                kind="image",
                prompt=brief.prompt,
                country_code=brief.country_code,
                aspect_ratio=brief.aspect_ratio,
                negative_prompt=brief.negative_prompt,
                reference_url=brief.reference_url,
                brand_context=brief.brand_context,
                seed=brief.seed,
                metadata=dict(brief.metadata or {}),
            )
        )
        image_job.provider = "yandexart_motion"
        image_job.kind = "video"
        image_job.model = f"{image_job.model}+motion"
        image_job.provider_payload["duration_seconds"] = max(
            2, min(int(brief.duration_seconds or 5), 15)
        )
        image_job.provider_payload["aspect_ratio"] = str(brief.aspect_ratio or "1:1")
        return image_job

    def poll(self, job: CreativeJob) -> CreativeJob:
        image_job = YandexArtProvider(self.config).poll(job)
        if image_job.status != "succeeded":
            return image_job
        image_path = image_job.asset_path
        duration = int(image_job.provider_payload.get("duration_seconds") or 5)
        aspect_ratio = str(image_job.provider_payload.get("aspect_ratio") or "1:1")
        try:
            video_path = _render_motion_video(
                self.config,
                image_path=image_path,
                operation_id=image_job.external_id,
                duration_seconds=duration,
                aspect_ratio=aspect_ratio,
            )
        except ProviderTransportError:
            image_job.kind = "video"
            image_job.provider = "yandexart_motion"
            image_job.status = "failed"
            image_job.error_code = "motion_render_failed"
            image_job.mime_type = ""
            image_job.asset_path = ""
            return image_job
        finally:
            if image_path:
                Path(image_path).unlink(missing_ok=True)
        image_job.kind = "video"
        image_job.provider = "yandexart_motion"
        image_job.mime_type = "video/mp4"
        image_job.asset_path = video_path
        image_job.error_code = ""
        image_job.status = "succeeded"
        return image_job


class GigaChatImageProvider:
    _IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self._token = ""
        self._token_expires_at = 0.0

    def supports(self, kind: str) -> bool:
        return kind == "image"

    def configured(self, kind: str) -> bool:
        return self.supports(kind) and bool(self.config.credentials and self.config.oauth_url)

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        body = urllib.parse.urlencode({"scope": self.config.scope or "GIGACHAT_API_PERS"}).encode("utf-8")
        _, _, raw = _request(
            "POST",
            self.config.oauth_url,
            headers={
                "Authorization": f"Basic {self.config.credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
            },
            body=body,
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_json_bytes,
            ca_bundle_file=self.config.ca_bundle_file,
        )
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderTransportError("gigachat_oauth_invalid_json") from exc
        token = str(obj.get("access_token") or "").strip() if isinstance(obj, dict) else ""
        if not token:
            raise ProviderTransportError("gigachat_oauth_missing_token")
        expires_raw = obj.get("expires_at") if isinstance(obj, dict) else None
        try:
            expires_at = float(expires_raw)
            if expires_at > 10_000_000_000:
                expires_at /= 1000.0
        except (TypeError, ValueError):
            expires_at = time.time() + 25 * 60
        self._token = token
        self._token_expires_at = max(time.time() + 60, expires_at)
        return token

    def submit(self, brief: CreativeBrief) -> CreativeJob:
        if not self.configured(brief.kind):
            raise ProviderTransportError("provider_not_configured")
        token = self._access_token()
        payload = {
            "model": self.config.model_image or "GigaChat-2-Pro",
            "messages": [{"role": "user", "content": f"Нарисуй: {brief.prompt}"}],
            "function_call": "auto",
        }
        data = _json_request(
            "POST",
            self.config.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            payload=payload,
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_json_bytes,
            ca_bundle_file=self.config.ca_bundle_file,
        )
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        message = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
        content = str(message.get("content") or "") if isinstance(message, dict) else ""
        match = self._IMG_RE.search(content)
        if not match:
            raise ProviderTransportError("gigachat_missing_image_id")
        file_id = match.group(1).strip()
        _, headers, raw = _request(
            "GET",
            self.config.base_url.rstrip("/") + f"/files/{urllib.parse.quote(file_id)}/content",
            headers={"Authorization": f"Bearer {token}", "Accept": "image/jpeg"},
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_media_bytes,
            ca_bundle_file=self.config.ca_bundle_file,
        )
        job = CreativeJob(
            provider="gigachat",
            kind="image",
            status="succeeded",
            external_id=file_id,
            model=self.config.model_image or "GigaChat-2-Pro",
            mime_type=headers.get("content-type", "image/jpeg"),
        )
        return _store_asset(self.config, job, raw)

    def poll(self, job: CreativeJob) -> CreativeJob:
        return job


class OpenAIVisualProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def supports(self, kind: str) -> bool:
        return kind in {"image", "video"}

    def configured(self, kind: str) -> bool:
        if not self.supports(kind) or not self.config.api_key or not self.config.base_url:
            return False
        if kind == "video":
            return bool(self.config.model_video)
        return bool(self.config.model_image)

    def submit(self, brief: CreativeBrief) -> CreativeJob:
        if brief.kind == "image":
            return self._submit_image(brief)
        return self._submit_video(brief)

    def _submit_image(self, brief: CreativeBrief) -> CreativeJob:
        model = self.config.model_image or "gpt-image-2"
        size = _openai_image_size(brief.aspect_ratio)
        data = _json_request(
            "POST",
            self.config.base_url.rstrip("/") + "/images/generations",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            payload={"model": model, "prompt": brief.prompt, "size": size, "n": 1},
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_json_bytes,
        )
        rows = data.get("data") if isinstance(data.get("data"), list) else []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        job = CreativeJob(provider="openai", kind="image", status="succeeded", model=model)
        encoded = str(row.get("b64_json") or "")
        if encoded:
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError, TypeError):
                job.status = "failed"
                job.error_code = "invalid_image_encoding"
                return job
            job.mime_type = "image/png"
            return _store_asset(self.config, job, raw)
        url = str(row.get("url") or "").strip()
        if url:
            job.media_url = url
            return _download_asset(self.config, job, url)
        job.status = "failed"
        job.error_code = "missing_image"
        return job

    def _submit_video(self, brief: CreativeBrief) -> CreativeJob:
        if brief.reference_url:
            raise ProviderTransportError("openai_video_reference_not_supported_by_gateway")
        model = self.config.model_video
        if not model:
            raise ProviderTransportError("openai_video_model_not_configured")
        fields = {
            "model": model,
            "prompt": brief.prompt,
            "seconds": str(_openai_video_seconds(brief.duration_seconds)),
            "size": _openai_video_size(brief.aspect_ratio),
        }
        body, content_type = _multipart(fields)
        _, _, raw = _request(
            "POST",
            self.config.base_url.rstrip("/") + "/videos",
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": content_type, "Accept": "application/json"},
            body=body,
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_json_bytes,
        )
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderTransportError("openai_video_invalid_json") from exc
        external_id = str(data.get("id") or "") if isinstance(data, dict) else ""
        if not external_id:
            raise ProviderTransportError("missing_video_id")
        status = _openai_status(str(data.get("status") or "queued"))
        return CreativeJob(provider="openai", kind="video", status=status, external_id=external_id, model=model)

    def poll(self, job: CreativeJob) -> CreativeJob:
        if job.kind == "image" or job.done:
            return job
        data = _json_request(
            "GET",
            self.config.base_url.rstrip("/") + f"/videos/{urllib.parse.quote(job.external_id)}",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_json_bytes,
        )
        job.status = _openai_status(str(data.get("status") or "queued"))
        if job.status == "failed":
            job.error_code = "provider_error"
            return job
        if job.status != "succeeded":
            return job
        _, headers, raw = _request(
            "GET",
            self.config.base_url.rstrip("/") + f"/videos/{urllib.parse.quote(job.external_id)}/content",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_media_bytes,
        )
        job.mime_type = headers.get("content-type", "video/mp4")
        return _store_asset(self.config, job, raw)


class RunwayVisualProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def supports(self, kind: str) -> bool:
        return kind in {"image", "video"}

    def configured(self, kind: str) -> bool:
        return self.supports(kind) and bool(self.config.api_key and self.config.base_url)

    def submit(self, brief: CreativeBrief) -> CreativeJob:
        model = (self.config.model_image if brief.kind == "image" else self.config.model_video) or (
            "gen4_image" if brief.kind == "image" else "gen4.5"
        )
        if brief.kind == "image":
            endpoint = "/text_to_image"
            payload: dict[str, Any] = {
                "model": model,
                "promptText": brief.prompt[:1000],
                "ratio": _runway_image_ratio(brief.aspect_ratio),
            }
        else:
            endpoint = "/image_to_video" if brief.reference_url else "/text_to_video"
            payload = {
                "model": model,
                "promptText": brief.prompt[:1000],
                "ratio": _runway_video_ratio(brief.aspect_ratio),
                "duration": max(2, min(brief.duration_seconds, 10)),
            }
            if brief.reference_url:
                payload["promptImage"] = brief.reference_url
            if brief.negative_prompt and model in {"veo3", "veo3.1", "veo3.1_fast"}:
                payload["negativePrompt"] = brief.negative_prompt[:1000]
        if brief.seed is not None:
            payload["seed"] = int(brief.seed)
        data = _json_request(
            "POST",
            self.config.base_url.rstrip("/") + endpoint,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "X-Runway-Version": "2024-11-06",
            },
            payload=payload,
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_json_bytes,
        )
        task_id = str(data.get("id") or "").strip()
        if not task_id:
            raise ProviderTransportError("missing_task_id")
        return CreativeJob(provider="runway", kind=brief.kind, status="queued", external_id=task_id, model=model)

    def poll(self, job: CreativeJob) -> CreativeJob:
        if job.done:
            return job
        data = _json_request(
            "GET",
            self.config.base_url.rstrip("/") + f"/tasks/{urllib.parse.quote(job.external_id)}",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "X-Runway-Version": "2024-11-06",
            },
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_json_bytes,
        )
        status = str(data.get("status") or "PENDING").upper()
        if status in {"SUCCEEDED", "SUCCESS"}:
            job.status = "succeeded"
            outputs = data.get("output") if isinstance(data.get("output"), list) else []
            url = str(outputs[0] or "") if outputs else ""
            if not url:
                job.status = "failed"
                job.error_code = "missing_output"
                return job
            job.media_url = url
            return _download_asset(self.config, job, url)
        if status in {"FAILED", "CANCELLED", "CANCELED"}:
            job.status = "failed"
            job.error_code = status.lower()
            return job
        job.status = "running"
        return job


class SelfHostedVisualProvider:
    """Stable local gateway contract for open-weight image/video models.

    The product does not import torch/diffusers. A GPU service can front Wan,
    Qwen-Image, FLUX or another model behind this small normalized HTTP API.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def supports(self, kind: str) -> bool:
        return kind in {"image", "video"}

    def configured(self, kind: str) -> bool:
        return self.supports(kind) and bool(self.config.base_url)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}

    def submit(self, brief: CreativeBrief) -> CreativeJob:
        payload: dict[str, Any] = {
            "kind": brief.kind,
            "prompt": brief.prompt,
            "negative_prompt": brief.negative_prompt,
            "aspect_ratio": brief.aspect_ratio,
            "duration_seconds": brief.duration_seconds,
            "reference_url": brief.reference_url,
            "seed": brief.seed,
        }
        selected_model = self.config.model_image if brief.kind == "image" else self.config.model_video
        if selected_model:
            payload["model"] = selected_model
        data = _json_request(
            "POST",
            self.config.base_url.rstrip("/") + "/v1/creative/generations",
            headers=self._headers(),
            payload=payload,
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_json_bytes,
        )
        job = _normalized_gateway_job(data, brief.kind, provider="selfhosted")
        return self._materialize(job, data)

    def poll(self, job: CreativeJob) -> CreativeJob:
        if job.done:
            return job
        data = _json_request(
            "GET",
            self.config.base_url.rstrip("/") + f"/v1/creative/generations/{urllib.parse.quote(job.external_id)}",
            headers=self._headers(),
            timeout=self.config.timeout_seconds,
            max_bytes=self.config.max_json_bytes,
        )
        refreshed = _normalized_gateway_job(data, job.kind, provider="selfhosted", fallback_id=job.external_id)
        return self._materialize(refreshed, data)

    def _materialize(self, job: CreativeJob, data: dict[str, Any]) -> CreativeJob:
        if job.status != "succeeded":
            return job
        encoded = str(data.get("media_base64") or data.get("b64_json") or "")
        if encoded:
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError, TypeError):
                job.status = "failed"
                job.error_code = "invalid_media_encoding"
                return job
            return _store_asset(self.config, job, raw)
        if job.media_url:
            return _download_asset(self.config, job, job.media_url)
        if job.asset_path:
            root = ensure_output_dir(self.config.output_dir).resolve()
            candidate = Path(job.asset_path).expanduser().resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                job.status = "failed"
                job.error_code = "unsafe_asset_path"
                return job
            if candidate.is_file():
                job.asset_path = str(candidate)
                return job
        job.status = "failed"
        job.error_code = "missing_output"
        return job


def _normalized_gateway_job(data: dict[str, Any], kind: str, *, provider: str, fallback_id: str = "") -> CreativeJob:
    raw_status = str(data.get("status") or "queued").strip().lower()
    mapping = {
        "pending": "queued",
        "queued": "queued",
        "processing": "running",
        "running": "running",
        "completed": "succeeded",
        "succeeded": "succeeded",
        "success": "succeeded",
        "failed": "failed",
        "error": "failed",
    }
    status = mapping.get(raw_status, "queued")
    return CreativeJob(
        provider=provider,
        kind=kind,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        external_id=str(data.get("id") or data.get("job_id") or fallback_id),
        model=str(data.get("model") or ""),
        mime_type=str(data.get("mime_type") or ""),
        asset_path=str(data.get("asset_path") or ""),
        media_url=str(data.get("media_url") or data.get("url") or ""),
        error_code=str(data.get("error_code") or "") if status == "failed" else "",
    )


def _multipart(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = "----visualcreative-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _openai_status(value: str) -> str:
    raw = value.strip().lower()
    if raw in {"completed", "succeeded", "success"}:
        return "succeeded"
    if raw in {"failed", "cancelled", "canceled"}:
        return "failed"
    if raw in {"in_progress", "processing", "running"}:
        return "running"
    return "queued"


def _openai_video_seconds(seconds: int) -> int:
    requested = int(seconds or 4)
    return min((4, 8, 12), key=lambda item: abs(item - requested))


def _openai_video_size(ratio: str) -> str:
    width, height = _ratio_pair(ratio)
    return "1280x720" if width >= height else "720x1280"


def _openai_image_size(ratio: str) -> str:
    width, height = _ratio_pair(ratio)
    if width == height:
        return "1024x1024"
    return "1536x1024" if width > height else "1024x1536"


def _runway_video_ratio(ratio: str) -> str:
    width, height = _ratio_pair(ratio)
    return "1280:720" if width >= height else "720:1280"


def _runway_image_ratio(ratio: str) -> str:
    width, height = _ratio_pair(ratio)
    requested = width / height
    candidates = (
        ("1024:1024", 1.0),
        ("1360:768", 1360 / 768),
        ("1080:1440", 1080 / 1440),
        ("1080:1920", 1080 / 1920),
    )
    return min(candidates, key=lambda item: abs(item[1] - requested))[0]
