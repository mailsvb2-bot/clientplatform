from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

from a1.domain.programs import ContentKind
from a1.runtime.secrets import EnvironmentCredentialProvider, SecretReferenceError
from a1.transport.media import HmacMediaGatewayResolver, MediaReferenceError
from a1.transport.telegram_http import AiohttpTelegramBotClient, TelegramBotApiError
from core.runtime_env import env_float, env_int


def _required_env(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"staging_configuration_missing:{name}")
    return value


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
    chat_id = _required_env("A1_STAGING_TELEGRAM_CHAT_ID")
    media_reference = _required_env("A1_STAGING_MEDIA_REFERENCE")
    gateway_base_url = _required_env("A1_MEDIA_GATEWAY_BASE_URL")
    telegram_base_url = str(
        os.getenv("A1_TELEGRAM_API_BASE_URL") or "https://api.telegram.org"
    ).strip()
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
