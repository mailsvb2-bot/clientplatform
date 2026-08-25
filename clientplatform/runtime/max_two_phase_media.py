from __future__ import annotations

import asyncio
import logging
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from clientplatform.domain.programs import ContentKind
from clientplatform.runtime.messenger_provider_clients import (
    MaxRuntimeClient,
    _materialize_media,
    _provider_message_id,
)
from runtime.messenger_max_sender import (
    MaxBotSender,
    _MEDIA_TOKEN_REJECT_CODES,
    _attachment_retry_delays,
    _max_error,
    _max_error_code,
    _max_retryable_http_error,
)
from runtime.messenger_transport_errors import (
    MessengerMediaNotReadyError,
    MessengerMediaTokenRejectedError,
    MessengerTransportError,
)
from services.messenger.media_assets import (
    get_cached_media_token,
    invalidate_media_token,
)
from services.messenger.provider_transport import (
    ProviderPermanentHTTPError,
    json_request,
)


LOGGER = logging.getLogger(__name__)
_MEDIA_TYPE_BY_KIND = {
    ContentKind.IMAGE: "image",
    ContentKind.AUDIO: "audio",
    ContentKind.VIDEO: "video",
    ContentKind.DOCUMENT: "file",
}


class MaxPreparedMediaNotReadyError(MessengerMediaNotReadyError):
    """MAX explicitly rejected the final write because attachment is not ready."""

    retryable = True
    provider_write_definitely_rejected = True


class MaxPreparedMediaTokenRejectedError(MessengerMediaTokenRejectedError):
    """MAX explicitly rejected the final write because the media token is invalid."""

    retryable = True
    provider_write_definitely_rejected = True


@dataclass(frozen=True, slots=True, repr=False)
class PreparedMaxRuntimeMedia:
    """Transient upload result that contains no durable business state."""

    external_subject: str
    media_type: str
    media_token: str = field(repr=False)
    source_path: Path = field(repr=False)
    temporary: bool = field(repr=False)


def _discard_temporary_file(path: Path) -> None:
    """Best-effort temp-file cleanup that never masks delivery truth."""

    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning(
            "temporary MAX media file cleanup failed; delivery state is unchanged",
            exc_info=True,
        )


async def _discard_temporary_token_cache(path: Path, *, media_type: str) -> None:
    """Best-effort cache cleanup that never masks send/cancellation truth."""

    try:
        await asyncio.to_thread(
            invalidate_media_token,
            "max",
            path,
            media_type=media_type,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # validator: allow-wide-except
        # Reviewed cleanup boundary: cache deletion must never overwrite the
        # already-known provider/durable delivery outcome.
        LOGGER.warning(
            "temporary MAX media-token cache cleanup failed; delivery state is unchanged",
            exc_info=True,
        )


async def _explicit_media_rejection(
    code: str,
    prepared: PreparedMaxRuntimeMedia,
) -> MessengerTransportError | None:
    normalized = str(code or "").strip().casefold()
    provider_code = normalized.removeprefix("max.send_text.")
    if provider_code == "attachment.not.ready":
        return MaxPreparedMediaNotReadyError(
            "MAX attachment is not ready",
            code="max.attachment.not_ready",
        )
    if provider_code in _MEDIA_TOKEN_REJECT_CODES:
        await asyncio.to_thread(
            invalidate_media_token,
            "max",
            prepared.source_path,
            media_type=prepared.media_type,
        )
        return MaxPreparedMediaTokenRejectedError(
            "MAX media token rejected",
            code=f"max.send_text.{provider_code}",
        )
    return None


async def _post_prepared_media_once(
    *,
    sender: MaxBotSender,
    prepared: PreparedMaxRuntimeMedia,
) -> dict[str, object]:
    """Perform the single raw POST /messages after the durable boundary.

    This intentionally uses the shared provider transport rather than
    ``MaxBotSender.send_text`` so the raw error ``code`` is inspected before the
    response's ``message`` field. MAX documents error responses such as
    ``attachment.not.ready`` with both fields, while successful responses expose
    a Message object under ``message``.
    """

    url = (
        f"{sender._api_base()}/messages?user_id="  # noqa: SLF001 - canonical retained provider origin
        f"{urllib.parse.quote(prepared.external_subject)}"
    )
    payload = {
        "text": "",
        "attachments": [
            {
                "type": prepared.media_type,
                "payload": {"token": prepared.media_token},
            }
        ],
    }
    try:
        data = await asyncio.to_thread(
            json_request,
            url,
            method="POST",
            headers={"Authorization": sender._token()},  # noqa: SLF001 - final boundary credential
            payload=payload,
            retries=1,
            ssl_context=sender._ssl_context(),  # noqa: SLF001 - retained MAX TLS policy
        )
    except ProviderPermanentHTTPError as exc:
        raise sender._permanent_http_error(exc) from exc  # noqa: SLF001 - retained error policy
    except OSError as exc:
        rate_limited = _max_retryable_http_error("send_text", exc)
        if rate_limited is not None:
            raise rate_limited from exc
        raise
    if not isinstance(data, dict):
        raise ValueError("MAX provider response is not an object")
    return data


class TwoPhaseMaxRuntimeClient(MaxRuntimeClient):
    """MAX runtime client with replay-safe preparation and one final message write.

    Phase 1 resolves/downloads the source, uploads bytes to MAX when needed and,
    for a fresh upload, waits the first bounded attachment-readiness interval.
    It never calls ``POST /messages``. Phase 2 receives only the prepared
    provider token and performs exactly one message creation request. The durable
    worker places its non-replay marker between those phases.
    """

    async def prepare_media(
        self,
        *,
        token: str,
        external_subject: str,
        kind: ContentKind,
        media: str,
        idempotency_key: str,
    ) -> PreparedMaxRuntimeMedia:
        del idempotency_key
        media_type = _MEDIA_TYPE_BY_KIND.get(kind)
        if media_type is None:
            raise ValueError(f"unsupported MAX media kind: {kind.value}")

        path, temporary = await _materialize_media(media)
        sender = MaxBotSender(token=token)
        preparation_succeeded = False
        try:
            cached_before_prepare = await asyncio.to_thread(
                get_cached_media_token,
                "max",
                path,
                media_type=media_type,
            )
            media_token = await sender._ensure_media_token(  # noqa: SLF001 - canonical split of retained provider primitive
                path,
                media_type=media_type,
            )
            if cached_before_prepare is None:
                first_ready_delay = _attachment_retry_delays()[0]
                if first_ready_delay:
                    await asyncio.sleep(first_ready_delay)
            if temporary:
                await _discard_temporary_token_cache(path, media_type=media_type)

            clean_subject = str(external_subject or "").strip()
            clean_token = str(media_token or "").strip()
            if not clean_subject or not clean_token:
                raise ValueError("MAX prepared media is incomplete")

            prepared = PreparedMaxRuntimeMedia(
                external_subject=clean_subject,
                media_type=media_type,
                media_token=clean_token,
                source_path=path,
                temporary=temporary,
            )
            preparation_succeeded = True
            return prepared
        finally:
            if temporary and not preparation_succeeded:
                try:
                    await _discard_temporary_token_cache(path, media_type=media_type)
                finally:
                    _discard_temporary_file(path)

    async def send_prepared_media(
        self,
        *,
        token: str,
        prepared: object,
    ) -> str:
        if not isinstance(prepared, PreparedMaxRuntimeMedia):
            raise ValueError("MAX prepared media has an invalid type")

        data = await _post_prepared_media_once(
            sender=MaxBotSender(token=token),
            prepared=prepared,
        )
        code = _max_error_code(data)
        mapped = await _explicit_media_rejection(code, prepared)
        if mapped is not None:
            raise mapped
        if data.get("error") or code:
            raise _max_error("send_text", code or "provider_error")

        message = data.get("message")
        if not isinstance(message, dict):
            raise ValueError("MAX provider response has no message object")
        message_id = _provider_message_id(message)
        if not message_id:
            raise ValueError("MAX provider response has no message id")
        return message_id

    async def release_prepared_media(self, prepared: object) -> None:
        if not isinstance(prepared, PreparedMaxRuntimeMedia):
            return
        if prepared.temporary:
            await _discard_temporary_token_cache(
                prepared.source_path,
                media_type=prepared.media_type,
            )
            _discard_temporary_file(prepared.source_path)


__all__ = [
    "MaxPreparedMediaNotReadyError",
    "MaxPreparedMediaTokenRejectedError",
    "PreparedMaxRuntimeMedia",
    "TwoPhaseMaxRuntimeClient",
]
