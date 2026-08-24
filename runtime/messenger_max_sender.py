from __future__ import annotations

import asyncio
import math
import os
import re
import ssl
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import settings
from runtime.messenger_transport_errors import (
    MessengerMediaNotReadyError,
    MessengerMediaTokenRejectedError,
    MessengerTransportError,
)
from services.messenger.media_assets import (
    get_cached_media_token,
    invalidate_media_token,
    store_media_token,
)
from services.messenger.provider_transport import (
    ProviderPermanentHTTPError,
    ProviderUploadURLRejected,
    json_request,
    multipart_upload,
)

MAX_API2_BASE_URL = "https://platform-api2.max.ru"
LEGACY_MAX_API_BASE_URLS = {
    "https://platform-api.max.ru",
    "https://botapi.max.ru",
}
_MEDIA_TOKEN_REJECT_CODES = {
    "attachment.invalid",
    "attachment.not.found",
    "attachment.not_found",
    "invalid_attachment",
    "invalid_token",
    "media.not.found",
}


class MaxProviderRateLimitError(MessengerTransportError):
    """MAX explicitly rejected a message before creating it due to rate limits."""

    retryable = True
    provider_write_definitely_rejected = True


def _attachment_retry_delays() -> tuple[float, ...]:
    raw = (os.getenv("MAX_ATTACHMENT_RETRY_DELAYS_SEC") or "0.5,1,2,4,8,16").strip()
    values: list[float] = []
    for part in raw.split(","):
        try:
            value = float(part.strip())
        except ValueError:
            continue
        if math.isfinite(value) and value >= 0:
            values.append(min(value, 60.0))
    return tuple(values) or (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)


def _max_error_code(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    code = data.get("code")
    error = data.get("error")
    if not code and isinstance(error, dict):
        code = error.get("code")
    return str(code or "").strip().casefold()[:120]


def _deployed_env() -> bool:
    return (os.getenv("APP_ENV") or getattr(settings, "APP_ENV", "") or "dev").strip().lower() in {
        "prod",
        "production",
        "stage",
        "staging",
    }


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _max_error(operation: str, code: str) -> MessengerTransportError:
    safe_code = str(code or "provider_error").strip().casefold().replace(" ", "_")[:120]
    return MessengerTransportError(
        f"MAX provider {operation} failed",
        code=f"max.{operation}.{safe_code}",
    )


def _max_retryable_http_error(
    operation: str,
    exc: BaseException,
) -> MessengerTransportError | None:
    try:
        status_code = int(getattr(exc, "code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code != 429:
        return None
    return MaxProviderRateLimitError(
        f"MAX provider {operation} was rate limited",
        code=f"max.{operation}.http_429",
    )


def _legacy_max_ui():
    """Load Metrotherapy presentation helpers only for legacy UI calls.

    Canonical ClientPlatform transport must remain dependency-light and must not
    inherit Metrotherapy menus/text normalization merely by importing the sender.
    """

    from runtime import messenger_max_ui

    return messenger_max_ui


@dataclass
class MaxBotSender:
    token: str | None = None
    api_base_url: str | None = None

    @staticmethod
    def _main_menu_attachment(*args: Any, **kwargs: Any):
        return _legacy_max_ui().main_menu_attachment(*args, **kwargs)

    @staticmethod
    def _demo_kind_attachment(*args: Any, **kwargs: Any):
        return _legacy_max_ui().demo_kind_attachment(*args, **kwargs)

    @staticmethod
    def _score_scale_attachment(*args: Any, **kwargs: Any):
        return _legacy_max_ui().score_scale_attachment(*args, **kwargs)

    def _token(self) -> str:
        token = (self.token or settings.MAX_BOT_TOKEN or "").strip()
        if not token:
            raise MessengerTransportError("MAX_BOT_TOKEN is empty", code="max.config.token_empty")
        return token

    def _api_base(self) -> str:
        base = (
            self.api_base_url
            or os.getenv("MAX_API_BASE_URL")
            or getattr(settings, "MAX_API_BASE_URL", "")
            or MAX_API2_BASE_URL
        )
        clean = str(base or "").strip().rstrip("/")
        if clean in LEGACY_MAX_API_BASE_URLS:
            clean = MAX_API2_BASE_URL
        if clean == MAX_API2_BASE_URL:
            return clean
        if not clean.startswith("https://"):
            raise MessengerTransportError(
                "MAX_API_BASE_URL must start with https://",
                code="max.config.https_required",
            )
        if _deployed_env() or not _truthy_env("ALLOW_CUSTOM_MAX_API_BASE_URL"):
            raise MessengerTransportError(
                "MAX_API_BASE_URL must use https://platform-api2.max.ru",
                code="max.config.official_api_required",
            )
        return clean

    def _ssl_context(self) -> ssl.SSLContext | None:
        bundle = str(os.getenv("MAX_CA_BUNDLE") or getattr(settings, "MAX_CA_BUNDLE", "") or "").strip()
        if not bundle:
            return None
        path = Path(bundle)
        if not path.is_file():
            raise MessengerTransportError(
                "MAX_CA_BUNDLE points to a missing file",
                code="max.config.ca_bundle_missing",
            )
        try:
            return ssl.create_default_context(cafile=str(path))
        except (OSError, ssl.SSLError) as exc:
            raise MessengerTransportError(
                f"MAX_CA_BUNDLE is invalid: {type(exc).__name__}",
                code="max.config.ca_bundle_invalid",
            ) from exc

    @staticmethod
    def _permanent_http_error(exc: ProviderPermanentHTTPError) -> MessengerTransportError:
        return MessengerTransportError(
            f"MAX provider HTTP {exc.status_code}",
            code=f"max.http.{exc.status_code}",
        )

    @staticmethod
    def _upload_payload(upload_meta: dict[str, Any], uploaded: Any, *, media_type: str) -> dict[str, Any]:
        if isinstance(uploaded, dict):
            if uploaded.get("token"):
                return {"token": str(uploaded["token"])}
            payload = uploaded.get("payload")
            if isinstance(payload, dict) and payload.get("token"):
                return {"token": str(payload["token"])}
            for key in (f"{media_type}_token", "file_token"):
                if uploaded.get(key):
                    return {"token": str(uploaded[key])}
        elif uploaded is not None:
            value = str(uploaded).strip()
            if value:
                return {"token": value}
        if isinstance(upload_meta, dict) and upload_meta.get("token"):
            return {"token": str(upload_meta["token"])}
        raise _max_error(f"{media_type}_upload", "token_missing")

    async def get_me(self) -> dict[str, Any]:
        token = self._token()
        try:
            data = await asyncio.to_thread(
                json_request,
                f"{self._api_base()}/me",
                method="GET",
                headers={"Authorization": token},
                payload=None,
                retries=1,
                ssl_context=self._ssl_context(),
            )
        except ProviderPermanentHTTPError as exc:
            raise self._permanent_http_error(exc) from exc
        if not isinstance(data, dict) or data.get("error"):
            raise _max_error("get_me", _max_error_code(data) or "provider_error")
        user_id = str(data.get("user_id") or "").strip()
        if not user_id.isdigit() or int(user_id) <= 0 or data.get("is_bot") is not True:
            raise _max_error("get_me", "invalid_bot_identity")
        return data

    async def ensure_webhook_subscription(
        self,
        *,
        url: str,
        secret: str,
        update_types: tuple[str, ...] = (
            "message_created",
            "message_callback",
            "bot_started",
        ),
    ) -> dict[str, Any]:
        target = str(url or "").strip()
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("MAX webhook URL must use HTTPS")
        try:
            explicit_port = parsed.port
        except ValueError as exc:
            raise ValueError("MAX webhook URL port is invalid") from exc
        if explicit_port is not None:
            raise ValueError("MAX webhook URL must use the default HTTPS port")
        clean_secret = str(secret or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{5,256}", clean_secret) is None:
            raise ValueError("MAX webhook secret format is invalid")
        events = tuple(dict.fromkeys(str(item or "").strip() for item in update_types))
        if not events or any(not item for item in events):
            raise ValueError("MAX webhook update types must not be empty")
        token = self._token()
        try:
            data = await asyncio.to_thread(
                json_request,
                f"{self._api_base()}/subscriptions",
                method="POST",
                headers={"Authorization": token},
                payload={
                    "url": target,
                    "update_types": list(events),
                    "secret": clean_secret,
                },
                retries=1,
                ssl_context=self._ssl_context(),
            )
        except ProviderPermanentHTTPError as exc:
            raise self._permanent_http_error(exc) from exc
        if not isinstance(data, dict) or data.get("success") is not True:
            raise _max_error(
                "subscription",
                _max_error_code(data) or "provider_rejected",
            )
        return data

    async def answer_callback(self, *, callback_id: str) -> dict[str, Any]:
        """Acknowledge one official ``message_callback`` update."""

        clean_callback_id = str(callback_id or "").strip()
        if not clean_callback_id or len(clean_callback_id) > 512:
            raise ValueError("MAX callback id format is invalid")
        if any(ord(char) < 32 or ord(char) == 127 for char in clean_callback_id):
            raise ValueError("MAX callback id format is invalid")
        token = self._token()
        try:
            data = await asyncio.to_thread(
                json_request,
                f"{self._api_base()}/answers?callback_id="
                f"{urllib.parse.quote(clean_callback_id, safe='')}",
                method="POST",
                headers={"Authorization": token},
                payload={},
                retries=1,
                ssl_context=self._ssl_context(),
            )
        except ProviderPermanentHTTPError as exc:
            raise self._permanent_http_error(exc) from exc
        if not isinstance(data, dict) or data.get("success") is not True:
            raise _max_error(
                "answer_callback",
                _max_error_code(data) or "provider_rejected",
            )
        return data

    async def send_text(self, external_user_id: str, text: str, **kwargs: Any):
        token = self._token()
        url = f"{self._api_base()}/messages?user_id={urllib.parse.quote(str(external_user_id))}"
        use_legacy_ui = bool(kwargs.pop("legacy_ui", True))
        if use_legacy_ui:
            max_ui = _legacy_max_ui()
            attachments = list(kwargs.get("attachments") or max_ui.native_keyboard_attachments(str(text or "")))
            prepared_text = max_ui.prepare_text(text, has_native_keyboard=bool(attachments))
        else:
            attachments = list(kwargs.get("attachments") or [])
            prepared_text = str(text or "")
        payload: dict[str, Any] = {"text": prepared_text}
        if attachments:
            payload["attachments"] = attachments
        if kwargs.get("disable_link_preview") is not None:
            url += f"&disable_link_preview={'true' if kwargs['disable_link_preview'] else 'false'}"
        if kwargs.get("format"):
            payload["format"] = kwargs["format"]
        if kwargs.get("notify") is not None:
            payload["notify"] = bool(kwargs["notify"])
        try:
            data = await asyncio.to_thread(
                json_request,
                url,
                method="POST",
                headers={"Authorization": token},
                payload=payload,
                retries=1,
                ssl_context=self._ssl_context(),
            )
        except ProviderPermanentHTTPError as exc:
            raise self._permanent_http_error(exc) from exc
        except OSError as exc:
            rate_limited = _max_retryable_http_error("send_text", exc)
            if rate_limited is not None:
                raise rate_limited from exc
            raise
        if isinstance(data, dict) and data.get("error"):
            raise _max_error("send_text", _max_error_code(data) or "provider_error")
        return data["message"] if isinstance(data, dict) and data.get("message") is not None else data

    async def _ensure_media_token(self, file_path: Path, *, media_type: str) -> str:
        cached = get_cached_media_token("max", file_path, media_type=media_type)
        if cached is not None:
            return cached.remote_token
        token = self._token()
        ssl_context = self._ssl_context()
        try:
            upload_meta = await asyncio.to_thread(
                json_request,
                f"{self._api_base()}/uploads?type={urllib.parse.quote(media_type)}",
                method="POST",
                headers={"Authorization": token},
                payload=None,
                retries=1,
                ssl_context=ssl_context,
            )
        except ProviderPermanentHTTPError as exc:
            raise self._permanent_http_error(exc) from exc
        upload_url = str(upload_meta.get("url") or "").strip()
        if not upload_url:
            raise _max_error(f"{media_type}_upload", "url_missing")
        try:
            uploaded = await asyncio.to_thread(
                multipart_upload,
                upload_url,
                field_name="data",
                path=file_path,
                ssl_context=ssl_context,
            )
        except ProviderPermanentHTTPError as exc:
            raise self._permanent_http_error(exc) from exc
        except ProviderUploadURLRejected as exc:
            raise _max_error(f"{media_type}_upload", exc.code) from exc
        media_token = str(self._upload_payload(upload_meta, uploaded, media_type=media_type).get("token") or "").strip()
        if not media_token:
            raise _max_error(f"{media_type}_upload", "token_missing")
        store_media_token("max", file_path, media_token, media_type=media_type)
        return media_token

    async def _send_media_payload(
        self,
        external_user_id: str,
        *,
        text: str,
        media_type: str,
        media_token: str,
        notify: bool | None = None,
    ) -> Any:
        token = self._token()
        url = f"{self._api_base()}/messages?user_id={urllib.parse.quote(str(external_user_id))}"
        payload: dict[str, Any] = {
            "text": text,
            "attachments": [{"type": media_type, "payload": {"token": media_token}}],
        }
        if notify is not None:
            payload["notify"] = bool(notify)
        delays = _attachment_retry_delays()
        last_error: Exception | None = None
        ssl_context = self._ssl_context()
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                data = await asyncio.to_thread(
                    json_request,
                    url,
                    method="POST",
                    headers={"Authorization": token},
                    payload=payload,
                    retries=1,
                    ssl_context=ssl_context,
                )
            except ProviderPermanentHTTPError as exc:
                raise self._permanent_http_error(exc) from exc
            except OSError as exc:
                rate_limited = _max_retryable_http_error(
                    f"send_{media_type}",
                    exc,
                )
                if rate_limited is not None:
                    raise rate_limited from exc
                raise
            except (ValueError, TypeError):
                # A connection failure after POST begins has an ambiguous write
                # outcome. Never repeat the message inside the provider adapter;
                # the durable MAX boundary will quarantine it for reconciliation.
                raise
            code = _max_error_code(data)
            if code == "attachment.not.ready":
                last_error = MessengerMediaNotReadyError(
                    "MAX attachment is not ready",
                    code="max.attachment.not_ready",
                )
                continue
            if code in _MEDIA_TOKEN_REJECT_CODES:
                raise MessengerMediaTokenRejectedError(
                    "MAX media token rejected",
                    code=f"max.attachment.{code}",
                )
            if isinstance(data, dict) and (data.get("error") or code):
                raise _max_error(f"send_{media_type}", code or "provider_error")
            return data.get("message", data) if isinstance(data, dict) else data
        if last_error is not None:
            if isinstance(last_error, MessengerTransportError):
                raise last_error
            raise _max_error(f"send_{media_type}", type(last_error).__name__.casefold())
        raise _max_error(f"send_{media_type}", "no_result")

    async def _send_media_file(
        self,
        external_user_id: str,
        file_path: Path,
        *,
        media_type: str,
        caption: str,
        notify: bool | None,
    ) -> Any:
        media_token = await self._ensure_media_token(file_path, media_type=media_type)
        try:
            return await self._send_media_payload(
                external_user_id,
                text=caption,
                media_type=media_type,
                media_token=media_token,
                notify=notify,
            )
        except MessengerMediaTokenRejectedError:
            await asyncio.to_thread(
                invalidate_media_token,
                "max",
                file_path,
                media_type=media_type,
            )
            fresh_token = await self._ensure_media_token(file_path, media_type=media_type)
            return await self._send_media_payload(
                external_user_id,
                text=caption,
                media_type=media_type,
                media_token=fresh_token,
                notify=notify,
            )

    async def send_image_file(
        self,
        external_user_id: str,
        file_path: Path,
        *,
        caption: str | None = None,
        **kwargs: Any,
    ):
        return await self._send_media_file(
            external_user_id,
            file_path,
            media_type="image",
            caption=caption or "",
            notify=kwargs.get("notify"),
        )

    async def send_audio_file(
        self,
        external_user_id: str,
        file_path: Path,
        *,
        caption: str | None = None,
        **kwargs: Any,
    ):
        return await self._send_media_file(
            external_user_id,
            file_path,
            media_type="audio",
            caption=caption or "",
            notify=kwargs.get("notify"),
        )

    async def send_video_file(
        self,
        external_user_id: str,
        file_path: Path,
        *,
        caption: str | None = None,
        **kwargs: Any,
    ):
        return await self._send_media_file(
            external_user_id,
            file_path,
            media_type="video",
            caption=caption or "",
            notify=kwargs.get("notify"),
        )

    async def send_document_file(
        self,
        external_user_id: str,
        file_path: Path,
        *,
        caption: str | None = None,
        **kwargs: Any,
    ):
        return await self._send_media_file(
            external_user_id,
            file_path,
            media_type="file",
            caption=caption or "",
            notify=kwargs.get("notify"),
        )
