from __future__ import annotations

import urllib.parse

from config.settings import settings

from services.messenger.platforms import MessengerPlatform


def _strip(v: str | None) -> str:
    return (v or '').strip()


def build_referral_payload(referrer_user_id: int) -> str:
    return f'ref_{int(referrer_user_id)}'


def build_site_payload(source: str = 'site') -> str:
    clean = ''.join(ch for ch in (source or 'site').strip().lower() if ch.isalnum() or ch in {'_', '-'})
    return clean or 'site'


def build_owner_entry_payload(source: str = 'site') -> str:
    """Return an explicit owner-control deep-link payload.

    `cpo_` is intentionally distinct from customer acquisition (`cpa_`) and
    customer invite (`cpj_`) payloads so official ClientPlatform entry can
    never be mistaken for a tenant customer route.
    """

    return f'cpo_{build_site_payload(source)}'


def build_bridge_payload(token: str) -> str:
    token_clean = (token or "").strip()
    return f"bridge_{token_clean}"


def build_gift_payload(code: str) -> str:
    code_clean = (code or '').strip()
    return f'gift_{code_clean}'


def _telegram_link(payload: str) -> str:
    username = _strip(settings.TELEGRAM_BOT_USERNAME)
    return f'https://t.me/{username}?start={urllib.parse.quote(payload)}' if username else ''


def _max_link(payload: str) -> str:
    bot_name = _strip(settings.MAX_BOT_NAME)
    base = _strip(settings.MAX_BOT_LINK_BASE)
    if not base:
        return ''
    if '{payload}' in base:
        return base.format(payload=urllib.parse.quote(payload), bot=urllib.parse.quote(bot_name))
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}start={urllib.parse.quote(payload)}'


def _vk_link(payload: str) -> str:
    group_id = _strip(settings.VK_GROUP_ID)
    if not group_id:
        return ''
    return f'https://vk.com/im?sel=-{group_id}&start={urllib.parse.quote(payload)}'


def _entry_targets(payload: str) -> list[dict[str, str]]:
    raw = [
        {
            'platform': MessengerPlatform.TELEGRAM.value,
            'title': 'Telegram',
            'url': _telegram_link(payload),
        },
        {
            'platform': MessengerPlatform.MAX.value,
            'title': 'MAX',
            'url': _max_link(payload),
        },
        {
            'platform': MessengerPlatform.VK.value,
            'title': 'ВКонтакте',
            'url': _vk_link(payload),
        },
    ]
    return [item for item in raw if item['url']]


def build_entry_targets(payload: str = 'site') -> list[dict[str, str]]:
    return _entry_targets(payload)


def build_site_entry_targets(source: str = 'site') -> list[dict[str, str]]:
    return _entry_targets(build_site_payload(source))


def build_owner_entry_targets(source: str = 'site') -> list[dict[str, str]]:
    return _entry_targets(build_owner_entry_payload(source))


def build_owner_entry_target(platform: str, source: str = 'site') -> dict[str, str] | None:
    expected = str(platform or '').strip().lower()
    return next(
        (item for item in build_owner_entry_targets(source) if item['platform'] == expected),
        None,
    )


def build_messenger_targets(referrer_user_id: int) -> list[dict[str, str]]:
    return _entry_targets(build_referral_payload(referrer_user_id))


def build_switch_targets(bridge_token: str) -> list[dict[str, str]]:
    return _entry_targets(build_bridge_payload(bridge_token))


def _telegram_share_url(target_url: str, text: str) -> str:
    return 'https://t.me/share/url?' + urllib.parse.urlencode({'url': target_url, 'text': text})


def _vk_share_url(target_url: str, text: str, *, title: str = 'Метротерапия') -> str:
    return 'https://vk.com/share.php?' + urllib.parse.urlencode({
        'url': target_url,
        'title': title,
        'comment': text,
    })


def _share_url_for_platform(platform: str, target_url: str, text: str, *, title: str = 'Метротерапия') -> str:
    if platform == MessengerPlatform.TELEGRAM.value:
        return _telegram_share_url(target_url, text)
    if platform == MessengerPlatform.VK.value:
        return _vk_share_url(target_url, text, title=title)
    # MAX currently uses the configured bot deep link as the canonical safe target.
    # If MAX adds or changes a public share-intent URL, only this function should change.
    return target_url


def build_share_targets(
    referrer_user_id: int,
    *,
    text: str,
    title: str = 'Метротерапия',
) -> list[dict[str, str]]:
    targets = build_messenger_targets(referrer_user_id)
    return [
        {
            **item,
            'entry_url': item['url'],
            'url': _share_url_for_platform(item['platform'], item['url'], text, title=title),
        }
        for item in targets
    ]


def build_gift_targets(code: str) -> list[dict[str, str]]:
    return _entry_targets(build_gift_payload(code))


def build_gift_share_targets(
    code: str,
    *,
    text: str,
    title: str = 'Подарок Метротерапии',
) -> list[dict[str, str]]:
    targets = build_gift_targets(code)
    return [
        {
            **item,
            'entry_url': item['url'],
            'url': _share_url_for_platform(item['platform'], item['url'], text, title=title),
        }
        for item in targets
    ]
