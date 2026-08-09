from __future__ import annotations

import asyncio
import json
import logging

from aiogram.types import Update

from clientplatform.application.bot_gateway import (
    ensure_telegram_customer_link,
    mark_ingress_event_processed,
    reschedule_ingress_event,
)
from clientplatform.application.partner_runtime import (
    record_partner_reply_if_expected,
)
from clientplatform.domain.bot_gateway import ClaimedIngressEvent
from clientplatform.runtime.bot_gateway import (
    ManagedBotGatewayRuntime as _ManagedBotGatewayRuntime,
    _safe_error_code,
    _telegram_actor,
)

log = logging.getLogger(__name__)


def _partner_reply_text(payload: dict[str, object]) -> str | None:
    for key in ("message", "edited_message"):
        raw = payload.get(key)
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or raw.get("caption") or "").strip()
        if text.startswith("/"):
            return None
        if text:
            return text[:4000]
        # A sticker/photo/voice reply is still a reply. Keep no binary/provider
        # payload, only a bounded semantic marker in the partner inbox.
        return "[сообщение без текста]"
    return None


class ManagedBotGatewayRuntime(_ManagedBotGatewayRuntime):
    """Managed bot gateway with partner-reply routing before CRM identity creation."""

    async def _process_item(self, item: ClaimedIngressEvent) -> None:
        try:
            if item.event.payload_json is None:
                raise ValueError("managed bot ingress payload is unavailable")
            payload = json.loads(item.event.payload_json)
            if not isinstance(payload, dict):
                raise ValueError("managed bot ingress payload must be an object")

            actor = _telegram_actor(payload)
            reply_text = _partner_reply_text(payload)
            if actor is not None and reply_text is not None:
                candidate_id = await asyncio.to_thread(
                    record_partner_reply_if_expected,
                    business_id=item.route.business_id,
                    connection_id=item.route.connection_id,
                    external_subject=str(actor[0]),
                    provider_event_key=item.event.provider_update_id,
                    reply_text=reply_text,
                )
                if candidate_id is not None:
                    await asyncio.to_thread(mark_ingress_event_processed, item)
                    self._processed += 1
                    return

            if actor is not None:
                await asyncio.to_thread(
                    ensure_telegram_customer_link,
                    route=item.route,
                    telegram_user_id=actor[0],
                    username=actor[1],
                    display_name=actor[2],
                )
            bot = await self._bot_for(item.route)
            try:
                update = Update.model_validate(payload, context={"bot": bot})
            except AttributeError:
                update = Update(**payload)
            await self._dispatcher.feed_webhook_update(
                bot,
                update,
                managed_bot_business_id=item.route.business_id,
                managed_bot_id=item.route.managed_bot_id,
                managed_bot_connection_id=item.route.connection_id,
            )
            await asyncio.to_thread(mark_ingress_event_processed, item)
            self._processed += 1
        except Exception as exc:  # validator: allow-wide-except
            error_code = _safe_error_code(exc)
            result = await asyncio.to_thread(
                reschedule_ingress_event,
                item,
                error_code=error_code,
                max_attempts=self.config.max_attempts,
            )
            if result.status.value == "dead":
                self._dead += 1
            else:
                self._retried += 1
            log.warning(
                "Managed bot update processing failed",
                extra={
                    "managed_bot_id": item.route.managed_bot_id,
                    "business_id": item.route.business_id,
                    "event_id": item.event.id,
                    "error_code": error_code,
                },
            )


__all__ = ["ManagedBotGatewayRuntime"]
