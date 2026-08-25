from __future__ import annotations

import asyncio
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
    MaxProviderRateLimitError,
    _MEDIA_TOKEN_REJECT_CODES,
)
from runtime.messenger_transport_errors import (
    MessengerMediaNotReadyError,
    MessengerMediaTokenRejectedError,
    MessengerTransportError,
)
from services.messenger.media_assets import invalidate_media_token


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


class TwoPhaseMaxRuntimeClient(MaxRuntimeClient):
    """MAX runtime client with replay-safe preparation and one final message write.

    Phase 1 may resolve/download the source and upload bytes to MAX, but it never
    calls ``POST /messages``. Phase 2 receives only the prepared provider token
    and performs exactly one message creation request. The durable worker places
    its non-replay marker between those phases.
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
        try:
            media_token = await sender._ensure_media_token(  # noqa: SLF001 - canonical split of retained provider primitive
                path,
                media_type=media_type,
            )
            if temporary:
                # Temporary download paths are unique per attempt and therefore
                # must not leave useless durable cache rows behind. The provider
                # token remains in this transient object for the final write.
                await asyncio.to_thread(
                    invalidate_media_token,
                    "max",
                    path,
                    media_type=media_type,
                )
        except (Exception, asyncio.CancelledError):
            if temporary:
                path.unlink(missing_ok=True)
            raise

        clean_subject = str(external_subject or "").strip()
        clean_token = str(media_token or "").strip()
        if not clean_subject or not clean_token:
            if temporary:
                path.unlink(missing_ok=True)
            raise ValueError("MAX prepared media is incomplete")
        return PreparedMaxRuntimeMedia(
            external_subject=clean_subject,
            media_type=media_type,
            media_token=clean_token,
            source_path=path,
            temporary=temporary,
        )

    async def send_prepared_media(
        self,
        *,
        token: str,
        prepared: object,
    ) -> str:
        if not isinstance(prepared, PreparedMaxRuntimeMedia):
            raise ValueError("MAX prepared media has an invalid type")

        sender = MaxBotSender(token=token)
        attachment = {
            "type": prepared.media_type,
            "payload": {"token": prepared.media_token},
        }
        try:
            # Retained send_text uses one provider attempt (retries=1). With the
            # upload already complete, this call is the only message-write
            # boundary after the worker's durable non-replay marker.
            result = await sender.send_text(
                prepared.external_subject,
                "",
                legacy_ui=False,
                attachments=[attachment],
            )
        except MaxProviderRateLimitError:
            raise
        except MessengerTransportError as exc:
            code = str(getattr(exc, "safe_code", "") or "").strip().casefold()
            if code == "max.send_text.attachment.not.ready":
                raise MaxPreparedMediaNotReadyError(
                    "MAX attachment is not ready",
                    code="max.attachment.not_ready",
                ) from exc
            rejected_codes = {
                f"max.send_text.{provider_code}"
                for provider_code in _MEDIA_TOKEN_REJECT_CODES
            }
            if code in rejected_codes:
                await asyncio.to_thread(
                    invalidate_media_token,
                    "max",
                    prepared.source_path,
                    media_type=prepared.media_type,
                )
                raise MaxPreparedMediaTokenRejectedError(
                    "MAX media token rejected",
                    code=code,
                ) from exc
            raise

        message_id = _provider_message_id(result)
        if not message_id:
            raise ValueError("MAX provider response has no message id")
        return message_id

    async def release_prepared_media(self, prepared: object) -> None:
        if not isinstance(prepared, PreparedMaxRuntimeMedia):
            return
        if prepared.temporary:
            prepared.source_path.unlink(missing_ok=True)


__all__ = [
    "MaxPreparedMediaNotReadyError",
    "MaxPreparedMediaTokenRejectedError",
    "PreparedMaxRuntimeMedia",
    "TwoPhaseMaxRuntimeClient",
]
