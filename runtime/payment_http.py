from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os

from aiohttp import web

from services.payments.checkout_intent import (
    CheckoutIntentError,
    checkout_intent_required,
    verify_checkout_intent,
)
from services.payments.terms import payment_terms_html
from services.payments.verified_reconciliation import (
    record_verified_yookassa_webhook,
    yookassa_webhook_http_status,
)
from services.payments.yookassa_checkout import YooKassaCheckoutError, create_yookassa_confirmation_url
from services.practice_token_contract import package_by_id

log = logging.getLogger(__name__)

_TOKEN_PAYMENT_KINDS = {"tokens", "practices", "practice_package"}
_LEGACY_PAYMENT_KINDS = {"subscription", "gift"}
_ALLOWED_PAYMENT_KINDS = _TOKEN_PAYMENT_KINDS | _LEGACY_PAYMENT_KINDS


async def payment_terms_web(_request: web.Request) -> web.Response:
    return web.Response(
        text=payment_terms_html(),
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


def legacy_public_payment_kinds_enabled() -> bool:
    raw = (os.getenv("ENABLE_LEGACY_PUBLIC_PAYMENT_KINDS") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _normalize_payment_kind(kind: str | None, package_id: str | None = None) -> str:
    normalized = (kind or "tokens").strip().casefold()
    if normalized not in _ALLOWED_PAYMENT_KINDS:
        normalized = "tokens"
    if (package_id or "").strip():
        return "tokens"
    return normalized


def _legacy_kind_error_response(kind: str) -> web.Response | None:
    if kind in _LEGACY_PAYMENT_KINDS and not legacy_public_payment_kinds_enabled():
        return web.Response(
            status=410,
            text="Legacy public payment kind is disabled. Use practice package checkout.",
            content_type="text/plain",
        )
    return None


def _create_yookassa_payment(
    *,
    source: str,
    external_user_id: str = "",
    user_id: str | int | None = None,
    kind: str = "tokens",
    package_id: str | None = None,
    gift_token: str | None = None,
    checkout_intent: str | None = None,
    **_: object,
) -> str:
    canonical_user_id = str(user_id or external_user_id or "").strip()
    messenger_external_user_id = str(external_user_id or canonical_user_id).strip()
    return create_yookassa_confirmation_url(
        source=source,
        user_id=canonical_user_id,
        external_user_id=messenger_external_user_id,
        kind=kind,
        package_id=package_id,
        gift_token=gift_token,
        checkout_intent=checkout_intent,
    )


def _is_prod() -> bool:
    return (os.getenv("APP_ENV", "dev") or "dev").strip().lower() in {"prod", "production"}


def _webhook_secret() -> str:
    return (
        os.getenv("YOOKASSA_WEBHOOK_SECRET")
        or os.getenv("PAYMENT_WEBHOOK_SECRET")
        or os.getenv("WEBHOOK_SECRET")
        or ""
    ).strip()


def _provided_secret(request: web.Request) -> str:
    header_secret = (
        request.headers.get("X-Metrotherapy-Webhook-Secret")
        or request.headers.get("X-Webhook-Secret")
        or ""
    ).strip()
    if header_secret:
        return header_secret

    if _is_prod():
        return ""
    return (request.query.get("secret") or "").strip()


def _webhook_secret_ok(request: web.Request) -> bool:
    """Validate an optional reverse-proxy secret without blocking native YooKassa webhooks.

    YooKassa does not send the project's private X-Metrotherapy-Webhook-Secret
    header. Grant-producing events are authenticated against YooKassa's API in
    record_verified_yookassa_webhook(), which is the canonical source-of-truth
    check. A configured custom header remains supported as defense in depth when
    a trusted reverse proxy injects it; a wrong supplied header is rejected.
    """

    actual = _provided_secret(request)
    if not actual:
        return True
    expected = _webhook_secret()
    if not expected:
        return False
    return hmac.compare_digest(actual, expected)


def _package_error_response(package_id: str) -> web.Response | None:
    if not (package_id or "").strip():
        return web.Response(
            status=400,
            text="Practice package is required.",
            content_type="text/plain",
        )
    try:
        package_by_id(package_id)
    except ValueError:
        return web.Response(
            status=400,
            text="Unknown practice package.",
            content_type="text/plain",
        )
    return None


def _user_id_error_response(user_id: str) -> web.Response | None:
    cleaned = (user_id or "").strip()
    if cleaned.isdigit() and int(cleaned) > 0:
        return None
    return web.Response(
        status=400,
        text="User id is required for practice package checkout.",
        content_type="text/plain",
    )


def _checkout_intent_error_response(
    *,
    intent: str,
    user_id: str,
    package_id: str,
    kind: str,
    source: str,
    amount_minor: int,
    currency: str,
    gift_token: str,
) -> web.Response | None:
    if not (checkout_intent_required() or str(intent or "").strip()):
        return None
    try:
        verify_checkout_intent(
            intent,
            expected_user_id=user_id,
            expected_package_id=package_id,
            expected_kind=kind,
            expected_source=source,
            expected_amount_minor=amount_minor,
            expected_currency=currency,
            expected_gift_token=gift_token or None,
        )
    except CheckoutIntentError as exc:
        log.warning(
            "Checkout intent rejected: source=%s package_id=%s reason=%s",
            source,
            package_id,
            exc,
        )
        return web.Response(
            status=403,
            text="Invalid or expired checkout intent. Error code: CHECKOUT_INTENT_INVALID",
            content_type="text/plain",
        )
    return None


async def pay_yookassa_web(request: web.Request) -> web.Response:
    source = (request.query.get("source") or "unknown").strip()[:32]
    user_id = (request.query.get("user_id") or "").strip()[:64]
    external_user_id = (request.query.get("external_user_id") or user_id).strip()[:64]
    package_id = (request.query.get("package_id") or "").strip()[:64]
    gift_token = (request.query.get("gift_token") or "").strip()[:80]
    intent = (request.query.get("intent") or "").strip()
    kind = _normalize_payment_kind(request.query.get("kind"), package_id)

    # Digital goods inside Telegram are permanently Stars-only. Reject old or
    # manually constructed Telegram YooKassa links even when a stale env flag is
    # accidentally enabled. VK, MAX and web continue through the code below.
    if source.casefold() == "telegram" and kind in _TOKEN_PAYMENT_KINDS:
        return web.Response(
            status=410,
            text="Оплата цифровых пакетов через ЮKassa в Telegram отключена. Используйте Telegram Stars.",
            content_type="text/plain",
        )

    legacy_error = _legacy_kind_error_response(kind)
    if legacy_error is not None:
        return legacy_error

    if kind in _TOKEN_PAYMENT_KINDS:
        user_error = _user_id_error_response(user_id)
        if user_error is not None:
            return user_error
        package_error = _package_error_response(package_id)
        if package_error is not None:
            return package_error
        package = package_by_id(package_id)
        amount_minor = int(package.price_rub) * 100
        currency = "RUB"
        intent_error = _checkout_intent_error_response(
            intent=intent,
            user_id=user_id,
            package_id=package_id,
            kind=kind,
            source=source,
            amount_minor=amount_minor,
            currency=currency,
            gift_token=gift_token,
        )
        if intent_error is not None:
            return intent_error

    try:
        confirmation_url = await asyncio.to_thread(
            _create_yookassa_payment,
            source=source,
            user_id=user_id,
            external_user_id=external_user_id,
            kind=kind,
            package_id=package_id or None,
            gift_token=gift_token or None,
            checkout_intent=intent or None,
        )
    except (YooKassaCheckoutError, ValueError, TypeError, OSError):
        log.exception("YooKassa web payment endpoint failed")
        return web.Response(
            status=500,
            text=(
                "Не удалось создать платёж YooKassa. Попробуйте ещё раз позже или обратитесь в поддержку. "
                "Код ошибки: PAYMENT_CREATE_FAILED"
            ),
            content_type="text/plain",
        )

    raise web.HTTPFound(location=confirmation_url)


async def yookassa_reconciliation_webhook(request: web.Request) -> web.Response:
    """Provider reconciliation endpoint."""

    if not _webhook_secret_ok(request):
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)

    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        return web.json_response({"ok": False, "error": f"bad_json:{type(exc).__name__}"}, status=400)

    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "bad_payload"}, status=400)

    result = await asyncio.to_thread(record_verified_yookassa_webhook, payload)
    status, retryable = yookassa_webhook_http_status(result)
    headers = {"Retry-After": "5"} if retryable else None
    return web.json_response(
        {
            "ok": result.ok,
            "provider": result.provider,
            "provider_payment_id": result.provider_payment_id,
            "payment_status": result.status,
            "event": result.event,
            "inserted": result.inserted,
            "problem": result.problem,
            "processing_status": result.processing_status,
            "side_effects_done": result.side_effects_done,
            "retryable": retryable,
        },
        status=status,
        headers=headers,
    )
