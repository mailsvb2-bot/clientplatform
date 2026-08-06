from __future__ import annotations

import asyncio
import html
import logging
import os
from typing import TYPE_CHECKING

from aiohttp import web

from clientplatform.application.ad_connections import (
    ad_connections_enabled,
    complete_yandex_direct_oauth,
    yandex_direct_provider_configured,
)
from clientplatform.domain.ad_connections import AdConnectionError
from clientplatform.integrations.yandex_direct import YandexDirectError

if TYPE_CHECKING:
    from aiogram import Bot

log = logging.getLogger(__name__)

_SCREEN_CODE_REDIRECT_URI = "https://oauth.yandex.ru/verification_code"


def _screen_code_flow_enabled() -> bool:
    return (
        str(os.getenv("CLIENTPLATFORM_AD_OAUTH_REDIRECT_URI") or "").strip()
        == _SCREEN_CODE_REDIRECT_URI
    )


def ad_oauth_http_enabled() -> bool:
    return (
        ad_connections_enabled()
        and yandex_direct_provider_configured()
        and not _screen_code_flow_enabled()
    )


def register_ad_oauth_routes(app: web.Application, *, bot: "Bot") -> None:
    if not ad_oauth_http_enabled():
        return
    app["clientplatform_ad_oauth_bot"] = bot
    app.router.add_get("/oauth/yandex-direct/callback", yandex_direct_oauth_callback)


async def yandex_direct_oauth_callback(request: web.Request) -> web.Response:
    state = (request.query.get("state") or "").strip()
    code = (request.query.get("code") or "").strip()
    provider_error = (request.query.get("error") or "").strip()
    if provider_error:
        return _page(
            title="Подключение отменено",
            message="Яндекс не предоставил доступ к рекламному кабинету. Вернитесь в Telegram и повторите подключение.",
            status=400,
        )
    if not state or not code:
        return _page(
            title="Некорректный ответ",
            message="В ответе Яндекса отсутствуют необходимые данные. Вернитесь в Telegram и повторите подключение.",
            status=400,
        )
    try:
        completion = await asyncio.to_thread(
            complete_yandex_direct_oauth,
            state=state,
            code=code,
        )
    except (AdConnectionError, YandexDirectError, ValueError):
        log.exception("Yandex Direct OAuth callback failed")
        return _page(
            title="Не удалось подключить кабинет",
            message="Доступ не сохранён. Вернитесь в Telegram и повторите подключение.",
            status=400,
        )
    except RuntimeError:
        log.exception("Yandex Direct OAuth runtime composition failed")
        return _page(
            title="Не удалось подключить кабинет",
            message="Доступ не сохранён. Вернитесь в Telegram и повторите подключение.",
            status=400,
        )

    bot = request.app.get("clientplatform_ad_oauth_bot")
    if bot is not None:
        try:
            await bot.send_message(
                completion.user_id,
                "✅ Яндекс Директ подключён\n\n"
                f"Кабинет: {completion.connection.external_login}\n"
                "Теперь рекламный черновик можно отправить в этот кабинет после Вашего подтверждения.",
            )
        except OSError:
            log.exception("Yandex Direct OAuth success notification transport failed")
        except RuntimeError:
            log.exception("Yandex Direct OAuth success notification runtime failed")

    return _page(
        title="Кабинет подключён",
        message="Яндекс Директ успешно подключён к ClientPlatform. Можно закрыть эту страницу и вернуться в Telegram.",
        status=200,
    )


def _page(*, title: str, message: str, status: int) -> web.Response:
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    document = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f4f7fb;margin:0;padding:32px;color:#14213d}}
main{{max-width:620px;margin:10vh auto;background:white;padding:32px;border-radius:20px;box-shadow:0 16px 50px rgba(20,33,61,.12)}}
h1{{font-size:28px;margin:0 0 16px}}p{{font-size:17px;line-height:1.55;margin:0}}
</style>
</head>
<body><main><h1>{safe_title}</h1><p>{safe_message}</p></main></body>
</html>"""
    return web.Response(text=document, content_type="text/html", status=status)


__all__ = [
    "ad_oauth_http_enabled",
    "register_ad_oauth_routes",
    "yandex_direct_oauth_callback",
]
