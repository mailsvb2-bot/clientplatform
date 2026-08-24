from __future__ import annotations

import os
from dataclasses import dataclass

from config.settings import settings
from runtime.ingress_flags import max_webhook_enabled, vk_webhook_enabled
from runtime.telegram_transport import telegram_runtime_enabled, telegram_transport
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


def _positive_integer(value: str) -> bool:
    if not value:
        return False
    try:
        return int(value) > 0
    except ValueError:
        return False


def build_setup_status() -> MessengerSetupStatus:
    public_base = _strip(getattr(settings, 'MESSENGER_PUBLIC_BASE_URL', ''))
    public_base_https = public_base.startswith('https://')
    deployed = _deployed_env()
    max_enabled = max_webhook_enabled()
    vk_enabled = vk_webhook_enabled()
    telegram_enabled = telegram_runtime_enabled()
    telegram_transport_mode = telegram_transport()
    telegram_webhook_enabled = telegram_enabled and telegram_transport_mode == 'webhook'
    telegram_ok = bool(
        not telegram_enabled
        or _strip(getattr(settings, 'TELEGRAM_BOT_USERNAME', ''))
    )

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
        and (not deployed or (public_base_https and max_secret))
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
    vk_group_id_valid = _positive_integer(vk_group_id)
    vk_configured = bool(
        public_base
        and vk_group_id_valid
        and vk_group_token
        and vk_confirmation
        and (not deployed or (public_base_https and vk_secret))
    )
    vk_ok = bool(not vk_enabled or vk_configured)

    telegram_public = _strip(
        getattr(settings, 'TELEGRAM_WEBHOOK_PUBLIC_BASE_URL', '')
    )
    telegram_public_https = telegram_public.startswith('https://')
    telegram_webhook_ok = bool(
        not telegram_webhook_enabled
        or (
            telegram_public
            and (not deployed or telegram_public_https)
        )
    )
    webhook_runtime_ok = bool(max_ok and vk_ok and telegram_webhook_ok)

    missing: list[str] = []
    warnings: list[str] = []
    if telegram_enabled and not telegram_ok:
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
        elif not vk_group_id_valid:
            missing.append('VK_GROUP_ID must be a positive integer')
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
    if public_base and not public_base_https:
        if deployed and (max_enabled or vk_enabled):
            missing.append('MESSENGER_PUBLIC_BASE_URL must use https://')
        elif max_enabled or vk_enabled:
            warnings.append(
                'MESSENGER_PUBLIC_BASE_URL должен использовать https:// вне локальной разработки.'
            )

    if telegram_webhook_enabled and not telegram_public:
        missing.append('TELEGRAM_WEBHOOK_PUBLIC_BASE_URL')
    if telegram_enabled and telegram_public and not telegram_public_https:
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
    telegram_enabled = telegram_runtime_enabled()
    lines = ['🔧 Настройка multi-messenger', '']
    if telegram_enabled:
        lines.append(f"Telegram runtime/referral links: {'✅' if status.telegram_ok else '❌'}")
    else:
        lines.append('Telegram runtime: выключен (native-only VK/MAX)')
    lines.append(
        'MAX legacy webhook: '
        + ('✅' if status.max_ok else '❌')
        + (' включён' if max_enabled else ' выключен')
    )
    lines.append(
        'VK legacy webhook: '
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
    lines.append('1) Пользователь открывает ClientPlatform в подключённом мессенджере.')
    lines.append('2) Bridge/ref payload связывает канал с канонической учётной записью, когда связь нужна.')
    lines.append('3) VK/MAX webhook фиксирует внешний user id на server-resolved tenant route.')
    lines.append('4) Ручной ввод VK ID / MAX ID пользователю не нужен.')
    lines.append('5) Для VK callback-кнопок в Callback API должен быть включён тип события message_event.')
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
