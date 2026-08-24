from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any, Mapping

from aiohttp import web

from clientplatform.application.messenger_channels import (
    consume_customer_channel_link,
    ensure_channel_customer,
    resolve_messenger_ingress_route,
)
from clientplatform.application.customer_activity import record_customer_contact
from clientplatform.application.sales_intelligence import (
    normalize_customer_message_text,
    record_customer_channel_message,
)
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.messenger_channels import (
    CustomerChannelLinkRejected,
    MessengerRouteNotFound,
    extract_customer_link_token,
)
from clientplatform.domain.sales_ai_jobs import messenger_source_order
from clientplatform.runtime.sales_ai_config import SalesAIRuntimeConfig
from clientplatform.runtime.secrets import EnvironmentCredentialProvider, SecretReferenceError
from runtime.messenger_payloads import (
    max_event_key,
    max_raw_message as _max_raw_message,
    vk_event_key,
    vk_raw_message as _vk_raw_message,
)
from services.messenger.webhook_dedupe import (
    claim_inbound_event,
    complete_inbound_event,
    fail_inbound_event,
    record_inbound_failure,
)

log = logging.getLogger(__name__)

_MAX_BODY_BYTES = 262_144


def _json_body(raw: bytes) -> dict[str, Any] | None:
    if not raw or len(raw) > _MAX_BODY_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_provider_event_id(raw_key: str) -> str:
    raw = str(raw_key or "").strip()
    if raw and len(raw) <= 150 and all(ord(char) >= 32 and ord(char) != 127 for char in raw):
        return raw
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sales_ai_runtime() -> tuple[bool, str]:
    try:
        config = SalesAIRuntimeConfig.from_env()
    except (TypeError, ValueError):
        log.warning("Sales AI configuration is invalid; channel ingress continues without AI", exc_info=True)
        return False, ""
    return config.enabled, config.consent_target


def _verify_vk(payload: Mapping[str, Any], *, expected_secret: str, external_route_id: str) -> bool:
    supplied = str(payload.get("secret") or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected_secret):
        return False
    group_id = str(payload.get("group_id") or "").strip()
    return bool(group_id and hmac.compare_digest(group_id, str(external_route_id)))


def _verify_max(request: web.Request, *, expected_secret: str) -> bool:
    supplied = str(
        request.headers.get("X-Max-Bot-Api-Secret")
        or request.headers.get("X-Max-Webhook-Secret")
        or ""
    ).strip()
    return bool(supplied and hmac.compare_digest(supplied, expected_secret))


async def _process_business_event(
    *,
    platform: ConnectionPlatform,
    route_id: str,
    request: web.Request,
) -> web.Response:
    try:
        route = await asyncio.to_thread(
            resolve_messenger_ingress_route,
            route_id=route_id,
            expected_platform=platform,
        )
    except (MessengerRouteNotFound, ValueError):
        raise web.HTTPNotFound(text="not found") from None

    credential_provider = EnvironmentCredentialProvider()
    try:
        expected_secret = await asyncio.to_thread(
            credential_provider.resolve,
            route.webhook_secret_reference,
        )
    except SecretReferenceError:
        log.error(
            "Canonical messenger route secret is unavailable",
            extra={"route_id": route.id, "platform": platform.value},
        )
        return web.Response(status=503, text="unavailable")

    raw = await request.read()
    payload = _json_body(raw)
    if payload is None:
        return web.Response(status=400, text="invalid")
    if platform == ConnectionPlatform.VK:
        if not _verify_vk(payload, expected_secret=expected_secret, external_route_id=route.external_route_id):
            return web.Response(status=403, text="forbidden")
        if str(payload.get("type") or "").strip() == "confirmation":
            reference = route.confirmation_code_reference
            if reference is None:
                return web.Response(status=503, text="unavailable")
            try:
                confirmation_code = str(
                    await asyncio.to_thread(credential_provider.resolve, reference)
                    or ""
                ).strip()
            except SecretReferenceError:
                log.error(
                    "Canonical VK confirmation code is unavailable",
                    extra={"route_id": route.id},
                )
                return web.Response(status=503, text="unavailable")
            if not confirmation_code:
                return web.Response(status=503, text="unavailable")
            return web.Response(text=confirmation_code)
        raw_event_key = vk_event_key(payload)
        extracted = _vk_raw_message(payload)
    else:
        if not _verify_max(request, expected_secret=expected_secret):
            return web.Response(status=403, text="forbidden")
        raw_event_key = max_event_key(payload)
        extracted = _max_raw_message(payload)

    scoped_event_key = f"{route.id}:{raw_event_key}"
    if extracted is None:
        failure = await asyncio.to_thread(
            record_inbound_failure,
            platform.value,
            scoped_event_key,
            payload,
            "canonical_customer_extraction_failed",
            max_attempts=5,
        )
        if failure.retryable:
            return web.Response(status=503, text="retry")
        return web.Response(text="ok")

    if not await asyncio.to_thread(
        claim_inbound_event,
        platform.value,
        scoped_event_key,
        payload,
    ):
        return web.Response(text="ok")

    external_subject, raw_text, display_name = extracted
    try:
        link_token = extract_customer_link_token(raw_text)
        if link_token is not None:
            identity = await asyncio.to_thread(
                consume_customer_channel_link,
                route=route,
                token=link_token,
                external_subject=external_subject,
                display_name=display_name,
            )
        else:
            identity = await asyncio.to_thread(
                ensure_channel_customer,
                route=route,
                external_subject=external_subject,
                display_name=display_name,
            )

        contact_recorded = await asyncio.to_thread(
            record_customer_contact,
            business_id=route.business_id,
            platform=platform.value,
            external_subject=identity.external_subject,
            display_name=display_name,
        )
        if not contact_recorded:
            raise ValueError("accepted messenger identity disappeared before activity update")

        message_text = normalize_customer_message_text(raw_text)
        if message_text is not None and link_token is None:
            ai_enabled, consent_target = _sales_ai_runtime()
            await asyncio.to_thread(
                record_customer_channel_message,
                business_id=route.business_id,
                customer_id=identity.customer_id,
                platform=platform.value,
                external_subject=identity.external_subject,
                source_ref=f"route:{route.id}",
                provider_event_id=_safe_provider_event_id(raw_event_key),
                source_order=messenger_source_order(payload, platform),
                message_text=message_text,
                runtime_ai_enabled=ai_enabled,
                runtime_ai_consent_target=consent_target,
            )
        await asyncio.to_thread(
            complete_inbound_event,
            platform.value,
            scoped_event_key,
            payload,
        )
        return web.Response(text="ok")
    except CustomerChannelLinkRejected:
        await asyncio.to_thread(
            fail_inbound_event,
            platform.value,
            scoped_event_key,
            payload,
            "customer_channel_link_rejected",
        )
        return web.Response(status=409, text="link_rejected")
    except Exception as exc:  # validator: allow-wide-except - provider must retry durable ingress
        await asyncio.to_thread(
            fail_inbound_event,
            platform.value,
            scoped_event_key,
            payload,
            type(exc).__name__,
        )
        log.exception(
            "Canonical messenger ingress failed",
            extra={
                "route_id": route.id,
                "business_id": route.business_id,
                "platform": platform.value,
            },
        )
        return web.Response(status=503, text="retry")


async def canonical_vk_webhook(request: web.Request) -> web.Response:
    return await _process_business_event(
        platform=ConnectionPlatform.VK,
        route_id=str(request.match_info.get("route_id") or ""),
        request=request,
    )


async def canonical_max_webhook(request: web.Request) -> web.Response:
    return await _process_business_event(
        platform=ConnectionPlatform.MAX,
        route_id=str(request.match_info.get("route_id") or ""),
        request=request,
    )


__all__ = ["canonical_max_webhook", "canonical_vk_webhook"]
