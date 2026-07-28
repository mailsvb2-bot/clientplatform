from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote, urlsplit

from a1.domain.programs import ContentKind
from a1.runtime.secrets import EnvironmentCredentialProvider, SecretReferenceError
from a1.transport.media import HmacMediaGatewayResolver, MediaReferenceError
from a1.transport.telegram_http import AiohttpTelegramBotClient, TelegramBotApiError
from core.runtime_env import env_float, env_int


JsonPayload = Mapping[str, object]
PostJson = Callable[[str, JsonPayload, float], Awaitable[tuple[int, Any]]]


def _required_env(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"staging_configuration_missing:{name}")
    return value


def _telegram_method_url(*, base_url: str, token: str, method: str) -> str:
    parsed = urlsplit(str(base_url or "").strip().rstrip("/"))
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("staging_telegram_api_requires_https")
    normalized_token = str(token or "").strip()
    if not normalized_token:
        raise RuntimeError("staging_telegram_token_missing")
    safe_token = quote(normalized_token, safe=":_-.")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/bot{safe_token}/{method}"


async def _aiohttp_post_json(
    url: str,
    payload: JsonPayload,
    timeout_seconds: float,
) -> tuple[int, Any]:
    try:
        import aiohttp
    except ImportError:
        raise RuntimeError("staging_http_dependency_missing") from None

    timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                json=dict(payload),
                allow_redirects=False,
            ) as response:
                status = int(response.status)
                try:
                    body = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                return status, body
    except asyncio.TimeoutError:
        raise RuntimeError("staging_telegram_discovery_timeout") from None
    except aiohttp.ClientError:
        raise RuntimeError("staging_telegram_discovery_transport_failure") from None


async def _telegram_result(
    *,
    base_url: str,
    token: str,
    method: str,
    payload: JsonPayload,
    timeout_seconds: float,
    post_json: PostJson | None = None,
) -> Any:
    sender = post_json or _aiohttp_post_json
    status, body = await sender(
        _telegram_method_url(base_url=base_url, token=token, method=method),
        payload,
        timeout_seconds,
    )
    if not isinstance(body, dict) or not bool(body.get("ok")):
        error_code: int | str = status
        if isinstance(body, dict):
            raw_code = body.get("error_code")
            if isinstance(raw_code, (int, str)) and not isinstance(raw_code, bool):
                error_code = raw_code
        raise RuntimeError(f"staging_telegram_{method}_failed:{error_code}")
    return body.get("result")


def _extract_start_chat_ids(updates: Any) -> tuple[str, ...]:
    if not isinstance(updates, list):
        raise RuntimeError("staging_telegram_updates_invalid")
    chat_ids: set[str] = set()
    for update in updates:
        if not isinstance(update, dict):
            continue
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        text = str(message.get("text") or "").strip()
        if not text:
            continue
        command = text.split(maxsplit=1)[0]
        if command != "/start" and not command.startswith("/start@"):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("type") or "") != "private":
            continue
        raw_chat_id = chat.get("id")
        if isinstance(raw_chat_id, bool) or not isinstance(raw_chat_id, (int, str)):
            continue
        chat_id = str(raw_chat_id).strip()
        if chat_id:
            chat_ids.add(chat_id)
    return tuple(sorted(chat_ids))


async def _resolve_chat_id(
    *,
    token: str,
    telegram_base_url: str,
    explicit_chat_id: str = "",
    post_json: PostJson | None = None,
) -> str:
    configured = str(explicit_chat_id or "").strip()
    if configured:
        return configured

    timeout_seconds = env_float(
        "A1_STAGING_TELEGRAM_DISCOVERY_TIMEOUT_SEC",
        20.0,
        minimum=1.0,
        maximum=120.0,
    )
    webhook = await _telegram_result(
        base_url=telegram_base_url,
        token=token,
        method="getWebhookInfo",
        payload={},
        timeout_seconds=timeout_seconds,
        post_json=post_json,
    )
    if not isinstance(webhook, dict):
        raise RuntimeError("staging_telegram_webhook_info_invalid")
    if str(webhook.get("url") or "").strip():
        raise RuntimeError("staging_telegram_bot_webhook_active")

    updates = await _telegram_result(
        base_url=telegram_base_url,
        token=token,
        method="getUpdates",
        payload={"timeout": 0, "allowed_updates": ["message"]},
        timeout_seconds=timeout_seconds,
        post_json=post_json,
    )
    chat_ids = _extract_start_chat_ids(updates)
    if not chat_ids:
        raise RuntimeError("staging_chat_not_discovered_send_start")
    if len(chat_ids) > 1:
        raise RuntimeError("staging_chat_ambiguous_set_explicit_chat_id")
    return chat_ids[0]


async def _probe_gateway(url: str) -> None:
    try:
        import aiohttp
    except ImportError:
        raise RuntimeError("staging_http_dependency_missing") from None
    timeout_seconds = env_float(
        "A1_STAGING_GATEWAY_TIMEOUT_SEC",
        20.0,
        minimum=1.0,
        maximum=120.0,
    )
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                headers={"Range": "bytes=0-0"},
                allow_redirects=False,
            ) as response:
                if int(response.status) not in {200, 206}:
                    raise RuntimeError(
                        f"staging_gateway_probe_failed:{int(response.status)}"
                    )
                body = await response.read()
                if not body:
                    raise RuntimeError("staging_gateway_probe_empty")
                if len(body) > 1 and int(response.status) == 206:
                    raise RuntimeError("staging_gateway_range_contract_invalid")
    except asyncio.TimeoutError:
        raise RuntimeError("staging_gateway_probe_timeout") from None
    except aiohttp.ClientError:
        raise RuntimeError("staging_gateway_probe_transport_failure") from None


async def run() -> None:
    provider = EnvironmentCredentialProvider()
    token_reference = str(
        os.getenv("A1_STAGING_TELEGRAM_TOKEN_REFERENCE")
        or "secret://env/A1_SECRET_STAGING_TELEGRAM_BOT_TOKEN"
    ).strip()
    signing_reference = str(
        os.getenv("A1_MEDIA_SIGNING_SECRET_REFERENCE")
        or "secret://env/A1_SECRET_MEDIA_SIGNING_KEY"
    ).strip()
    token = provider.resolve(token_reference)
    media_reference = _required_env("A1_STAGING_MEDIA_REFERENCE")
    gateway_base_url = _required_env("A1_MEDIA_GATEWAY_BASE_URL")
    telegram_base_url = str(
        os.getenv("A1_TELEGRAM_API_BASE_URL") or "https://api.telegram.org"
    ).strip()
    chat_id = await _resolve_chat_id(
        token=token,
        telegram_base_url=telegram_base_url,
        explicit_chat_id=str(os.getenv("A1_STAGING_TELEGRAM_CHAT_ID") or ""),
    )
    parsed_gateway = urlsplit(gateway_base_url)
    if parsed_gateway.scheme != "https" or not parsed_gateway.netloc:
        raise RuntimeError("staging_gateway_requires_https")

    resolver = HmacMediaGatewayResolver(
        base_url=gateway_base_url,
        credential_provider=provider,
        signing_secret_reference=signing_reference,
        ttl_seconds=env_int(
            "A1_MEDIA_URL_TTL_SEC",
            300,
            minimum=60,
            maximum=900,
        ),
    )
    signed_url = await resolver.resolve(media_reference, ContentKind.AUDIO)
    await _probe_gateway(signed_url)

    client = AiohttpTelegramBotClient(
        base_url=telegram_base_url,
        timeout_seconds=env_float(
            "A1_TELEGRAM_HTTP_TIMEOUT_SEC",
            20.0,
            minimum=1.0,
            maximum=120.0,
        ),
    )
    message_id = await client.send_audio(
        token=token,
        chat_id=chat_id,
        audio=signed_url,
    )
    if not message_id:
        raise RuntimeError("staging_telegram_message_id_missing")
    print(f"A1 Telegram staging smoke passed; message_id={message_id}")


def main() -> int:
    try:
        asyncio.run(run())
    except SecretReferenceError as exc:
        print(f"A1 Telegram staging smoke failed: {type(exc).__name__}")
        return 1
    except MediaReferenceError as exc:
        print(f"A1 Telegram staging smoke failed: {type(exc).__name__}")
        return 1
    except TelegramBotApiError as exc:
        print(f"A1 Telegram staging smoke failed: {exc.code}")
        return 1
    except RuntimeError as exc:
        print(f"A1 Telegram staging smoke failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
