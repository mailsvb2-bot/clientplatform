from __future__ import annotations

import hashlib
import html

from aiohttp import web

from clientplatform.application.existing_bot_onboarding import (
    connect_existing_telegram_bot,
)
from clientplatform.application.native_messenger_onboarding import (
    provision_max_channel,
    provision_vk_channel,
)
from clientplatform.application.native_messenger_setup import (
    consume_native_messenger_setup,
    inspect_native_messenger_setup,
)
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.infrastructure.native_messenger_setup_repository import (
    NativeMessengerSetupRejected,
)
from config.settings import settings


_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
}


def _page(*, title: str, body: str, status: int = 200) -> web.Response:
    document = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{font-family:system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 20px;line-height:1.45}}
label{{display:block;margin:18px 0 6px}}input{{box-sizing:border-box;width:100%;padding:12px;font-size:16px}}
button{{margin-top:22px;padding:12px 18px;font-size:16px}}.note{{color:#555}}.ok{{font-weight:700}}
</style></head><body>{body}</body></html>"""
    return web.Response(
        text=document,
        content_type="text/html",
        status=status,
        headers=_SECURITY_HEADERS,
    )


def _token(request: web.Request) -> str:
    return str(request.match_info.get("token") or "").strip()


def _provider_setup_failure_page() -> web.Response:
    return _page(
        title="Не удалось подключить",
        body=(
            "<h1>Не удалось подключить мессенджер</h1>"
            "<p>ClientPlatform не смог подтвердить аккаунт или настроить Webhook. "
            "Вернитесь в раздел «Мессенджеры» и создайте новую одноразовую ссылку.</p>"
        ),
        status=502,
    )


def _setup_form(*, business_name: str, platform: ConnectionPlatform) -> str:
    labels = {
        ConnectionPlatform.TELEGRAM: "Telegram",
        ConnectionPlatform.VK: "ВКонтакте",
        ConnectionPlatform.MAX: "MAX",
    }
    label = labels[platform]
    group_field = ""
    if platform == ConnectionPlatform.VK:
        group_field = (
            '<label for="group_id">ID сообщества ВКонтакте</label>'
            '<input id="group_id" name="group_id" inputmode="numeric" '
            'autocomplete="off" required maxlength="32">'
        )
    return (
        f"<h1>Подключить {label}</h1>"
        f"<p>Бизнес: <strong>{html.escape(business_name)}</strong></p>"
        '<p class="note">Токен отправляется напрямую в ClientPlatform по HTTPS, '
        "не проходит через Telegram и после проверки хранится только в зашифрованном виде.</p>"
        '<form method="post" autocomplete="off">'
        f"{group_field}"
        f'<label for="provider_token">Токен {label}</label>'
        '<input id="provider_token" name="provider_token" type="password" '
        'autocomplete="new-password" required maxlength="4096">'
        f'<button type="submit">Подключить {label}</button>'
        "</form>"
        '<p class="note">Ссылка одноразовая и действует ограниченное время.</p>'
    )


async def native_messenger_setup_get(request: web.Request) -> web.Response:
    try:
        grant = inspect_native_messenger_setup(token=_token(request))
    except NativeMessengerSetupRejected:
        return _page(
            title="Ссылка недействительна",
            body="<h1>Ссылка недействительна</h1><p>Создайте новую ссылку в ClientPlatform.</p>",
            status=404,
        )
    return _page(
        title="Подключение мессенджера",
        body=_setup_form(
            business_name=grant.business_name,
            platform=grant.platform,
        ),
    )


async def native_messenger_setup_post(request: web.Request) -> web.Response:
    token = _token(request)
    try:
        preview = inspect_native_messenger_setup(token=token)
    except NativeMessengerSetupRejected:
        return _page(
            title="Ссылка недействительна",
            body="<h1>Ссылка недействительна</h1><p>Создайте новую ссылку в ClientPlatform.</p>",
            status=404,
        )
    if request.content_type != "application/x-www-form-urlencoded":
        return _page(
            title="Неверный запрос",
            body="<h1>Неверный запрос</h1><p>Откройте ссылку заново и используйте форму подключения.</p>",
            status=415,
        )
    form = await request.post()
    provider_token = str(form.get("provider_token") or "").strip()
    if not provider_token or len(provider_token) > 4096:
        return _page(
            title="Проверьте токен",
            body=_setup_form(
                business_name=preview.business_name,
                platform=preview.platform,
            ),
            status=400,
        )
    group_id = str(form.get("group_id") or "").strip()
    if preview.platform == ConnectionPlatform.VK and (
        not group_id.isdigit() or int(group_id) <= 0
    ):
        return _page(
            title="Проверьте ID сообщества",
            body=_setup_form(
                business_name=preview.business_name,
                platform=preview.platform,
            ),
            status=400,
        )

    try:
        grant = consume_native_messenger_setup(token=token)
        public_base = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip()
        if grant.platform == ConnectionPlatform.VK:
            result = await provision_vk_channel(
                actor=grant.actor,
                group_id=group_id,
                provider_token=provider_token,
                public_base_url=public_base,
            )
            label = "ВКонтакте"
            ready_name = result.display_name or result.username or "Канал"
        elif grant.platform == ConnectionPlatform.MAX:
            result = await provision_max_channel(
                actor=grant.actor,
                provider_token=provider_token,
                public_base_url=public_base,
            )
            label = "MAX"
            ready_name = result.display_name or result.username or "Канал"
        else:
            telegram_result = await connect_existing_telegram_bot(
                actor=grant.actor,
                token=provider_token,
                idempotency_key=(
                    "secure-setup:"
                    + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
                ),
            )
            label = "Telegram"
            ready_name = telegram_result.verified_username or "Бот"
    except NativeMessengerSetupRejected:
        return _page(
            title="Ссылка уже использована",
            body="<h1>Ссылка уже использована</h1><p>Создайте новую ссылку в ClientPlatform.</p>",
            status=409,
        )
    except (ValueError, OSError):
        return _provider_setup_failure_page()
    except RuntimeError:
        return _provider_setup_failure_page()

    return _page(
        title=f"{label} подключён",
        body=(
            f'<h1 class="ok">{label} подключён ✅</h1>'
            f"<p>{html.escape(ready_name)} готов к работе.</p>"
            "<p>Можно закрыть эту страницу и вернуться в ClientPlatform.</p>"
        ),
    )


__all__ = ["native_messenger_setup_get", "native_messenger_setup_post"]
