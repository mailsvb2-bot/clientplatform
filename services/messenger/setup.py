from __future__ import annotations

import os
from dataclasses import dataclass

from config.settings import settings
from runtime.ingress_flags import max_webhook_enabled, vk_webhook_enabled
from runtime.telegram_transport import telegram_transport
from services.messenger.bridge import issue_bridge_token
from services.messenger.links import build_switch_targets, build_messenger_targets


@dataclass(frozen=True)
class MessengerSetupStatus:
    telegram_ok: bool
    max_ok: bool
    vk_ok: bool
    webhook_runtime_ok: bool
    public_base_url: str
    vk_webhook_url: str
    max_webhook_url: str
    missing: tuple[str, ...]
    warnings: tuple[str, ...]


def _strip(value: str | None) -> str:
    return (value or '').strip().rstrip('/')


def _app_env() -> str:
    return (os.getenv('APP_ENV') or getattr(settings, 'APP_ENV', '') or 'dev').strip().lower()


def _deployed_env() -> bool:
    return _app_env() in {'prod', 'production', 'stage', 'staging'}


def _present(*values: object) -> bool:
    return any(_strip(str(value or '')) for value in values)


def build_setup_status() -> MessengerSetupStatus:
    public_base = _strip(getattr(settings, 'MESSENGER_PUBLIC_BASE_URL', ''))
    deployed = _deployed_env()
    max_enabled = max_webhook_enabled()
    vk_enabled = vk_webhook_enabled()
    telegram_transport_mode = telegram_transport()
    telegram_webhook_enabled = telegram_transport_mode == 'webhook'
    telegram_ok = bool(_strip(getattr(settings, 'TELEGRAM_BOT_USERNAME', '')))

    max_link = _strip(getattr(settings, 'MAX_BOT_LINK_BASE', ''))
    max_token = _strip(getattr(settings, 'MAX_BOT_TOKEN', ''))
    max_secret = _strip(getattr(settings, 'MAX_WEBHOOK_SECRET', ''))
    max_values_present = _present(
        max_link,
        max_token,
        max_secret,
        getattr(settings, 'MAX_BOT_NAME', ''),
    )
    max_configured = bool(
        public_base
        and max_link
        and max_token
        and (not deployed or max_secret)
    )
    max_ok = bool(not max_enabled or max_configured)

    vk_group_id = _strip(getattr(settings, 'VK_GROUP_ID', ''))
    vk_group_token = _strip(getattr(settings, 'VK_GROUP_TOKEN', ''))
    vk_confirmation = _strip(getattr(settings, 'VK_CONFIRMATION_TOKEN', ''))
    vk_secret = _strip(getattr(settings, 'VK_SECRET', ''))
    vk_values_present = _present(
        vk_group_id,
        vk_group_token,
        vk_confirmation,
        vk_secret,
    )
    vk_configured = bool(
        public_base
        and vk_group_id
        and vk_group_token
        and vk_confirmation
        and (not deployed or vk_secret)
    )
    vk_ok = bool(not vk_enabled or vk_configured)

    telegram_public = _strip(
        getattr(settings, 'TELEGRAM_WEBHOOK_PUBLIC_BASE_URL', '')
    )
    telegram_webhook_ok = bool(
        not telegram_webhook_enabled or telegram_public
    )
    webhook_runtime_ok = bool(max_ok and vk_ok and telegram_webhook_ok)

    missing: list[str] = []
    warnings: list[str] = []
    if not telegram_ok:
        missing.append('TELEGRAM_BOT_USERNAME')

    if max_enabled:
        if not public_base:
            missing.append('MESSENGER_PUBLIC_BASE_URL')
        if not max_token:
            missing.append('MAX_BOT_TOKEN')
        if not max_link:
            missing.append('MAX_BOT_LINK_BASE')
        if deployed and not max_secret:
            missing.append('MAX_WEBHOOK_SECRET')
    elif max_values_present:
        warnings.append(
            'Настройки MAX присутствуют, но MAX_WEBHOOK_ENABLED выключен; канал не запускается.'
        )

    if vk_enabled:
        if not public_base:
            missing.append('MESSENGER_PUBLIC_BASE_URL')
        if not vk_group_id:
            missing.append('VK_GROUP_ID')
        if not vk_group_token:
            missing.append('VK_GROUP_TOKEN')
        if not vk_confirmation:
            missing.append('VK_CONFIRMATION_TOKEN')
        if deployed and not vk_secret:
            missing.append('VK_SECRET')
    elif vk_values_present:
        warnings.append(
            'Настройки VK присутствуют, но VK_WEBHOOK_ENABLED выключен; канал не запускается.'
        )

    if max_enabled and max_link and '{payload}' not in max_link:
        warnings.append(
            'MAX_BOT_LINK_BASE не содержит {payload}; проект добавит ?start=..., но шаблон с {payload} надёжнее.'
        )
    if vk_enabled and vk_group_id:
        try:
            if int(vk_group_id) <= 0:
                raise ValueError('group id is not positive')
        except ValueError:
            missing.append('VK_GROUP_ID must be a positive integer')
    if public_base and not public_base.startswith('https://'):
        if deployed and (max_enabled or vk_enabled):
            missing.append('MESSENGER_PUBLIC_BASE_URL must use https://')
        elif max_enabled or vk_enabled:
            warnings.append(
                'MESSENGER_PUBLIC_BASE_URL должен использовать https:// вне локальной разработки.'
            )

    if telegram_webhook_enabled and not telegram_public:
        missing.append('TELEGRAM_WEBHOOK_PUBLIC_BASE_URL')
    if telegram_public and not telegram_public.startswith('https://'):
        if deployed and telegram_webhook_enabled:
            missing.append('TELEGRAM_WEBHOOK_PUBLIC_BASE_URL must use https://')
        elif telegram_webhook_enabled:
            warnings.append(
                'TELEGRAM_WEBHOOK_PUBLIC_BASE_URL должен использовать https:// вне локальной разработки.'
            )

    vk_webhook_url = f'{public_base}/webhooks/vk' if public_base else ''
    max_webhook_url = f'{public_base}/webhooks/max' if public_base else ''
    return MessengerSetupStatus(
        telegram_ok=telegram_ok,
        max_ok=max_ok,
        vk_ok=vk_ok,
        webhook_runtime_ok=webhook_runtime_ok,
        public_base_url=public_base,
        vk_webhook_url=vk_webhook_url,
        max_webhook_url=max_webhook_url,
        missing=tuple(dict.fromkeys(missing)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def render_setup_text() -> str:
    status = build_setup_status()
    max_enabled = max_webhook_enabled()
    vk_enabled = vk_webhook_enabled()
    lines = ['🔧 Настройка multi-messenger', '']
    lines.append(f"Telegram referral/switch links: {'✅' if status.telegram_ok else '❌'}")
    lines.append(
        'MAX webhook: '
        + ('✅' if status.max_ok else '❌')
        + (' включён' if max_enabled else ' выключен')
    )
    lines.append(
        'VK webhook: '
        + ('✅' if status.vk_ok else '❌')
        + (' включён' if vk_enabled else ' выключен')
    )
    lines.append(f"Webhook runtime contract: {'✅' if status.webhook_runtime_ok else '❌'}")
    lines.append('')
    if status.public_base_url and (max_enabled or vk_enabled):
        lines.append(f'Public base URL: {status.public_base_url}')
        if vk_enabled:
            lines.append(f'VK webhook URL: {status.vk_webhook_url}')
        if max_enabled:
            lines.append(f'MAX webhook URL: {status.max_webhook_url}')
        lines.append('')
    lines.append('Как это работает:')
    lines.append('1) Пользователь в Telegram нажимает переход в VK/MAX.')
    lines.append('2) Открывается ссылка с start-параметром bridge/ref.')
    lines.append('3) Включённый VK/MAX webhook получает входящее сообщение и фиксирует внешний user id.')
    lines.append('4) Ручной ввод VK ID / MAX ID пользователю не нужен.')
    lines.append('5) Для новых VK callback-кнопок в Callback API должен быть включён тип события message_event.')
    lines.append('')
    if status.missing:
        lines.append('Не хватает переменных для включённых каналов:')
        for item in status.missing:
            lines.append(f'• {item}')
    else:
        lines.append('Все включённые messenger-каналы настроены; выключенные каналы не считаются ошибкой.')
    if status.warnings:
        lines.append('')
        lines.append('Предупреждения:')
        for item in status.warnings:
            lines.append(f'• {item}')
    return '\n'.join(lines)


def render_setup_links_preview(user_id: int) -> str:
    token = issue_bridge_token(int(user_id), purpose='switch')
    switch_targets = build_switch_targets(token)
    referral_targets = build_messenger_targets(int(user_id))
    lines = ['🔗 Предпросмотр ссылок', '']
    if switch_targets:
        lines.append('Переход в другой мессенджер:')
        for item in switch_targets:
            lines.append(f"• {item['title']}: {item['url']}")
        lines.append('')
    else:
        lines.append('Ссылки перехода пока не строятся — соответствующие каналы выключены или не настроены.')
        lines.append('')
    if referral_targets:
        lines.append('Реферальные / share ссылки:')
        for item in referral_targets:
            lines.append(f"• {item['title']}: {item['url']}")
    else:
        lines.append('Реферальные / share ссылки пока не строятся.')
    lines.append('')
    lines.append('Пользователю не нужно вручную вводить VK ID / MAX ID: внешний id фиксируется entrypoint/webhook-слоем после перехода по ссылке.')
    return '\n'.join(lines)


def validate_setup(strict: bool = False) -> tuple[bool, str]:
    status = build_setup_status()
    text = render_setup_text()
    ok = not status.missing
    if strict and status.warnings:
        ok = False
    return ok, text
