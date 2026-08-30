from services.messenger.links import (
    build_gift_share_targets,
    build_gift_targets,
    build_messenger_targets,
    build_owner_entry_payload,
    build_owner_entry_target,
    build_owner_entry_targets,
    build_share_targets,
    build_site_entry_targets,
)
from config.settings import settings


def _configure_all_targets(monkeypatch):
    monkeypatch.setattr(settings, 'TELEGRAM_BOT_USERNAME', 'metro_test_bot')
    monkeypatch.setattr(settings, 'MAX_BOT_LINK_BASE', 'https://max.ru/metrotherapy')
    monkeypatch.setattr(settings, 'MAX_BOT_NAME', 'metrotherapy_bot')
    monkeypatch.setattr(settings, 'VK_GROUP_ID', '123456')


def test_build_targets_uses_available_messengers(monkeypatch):
    _configure_all_targets(monkeypatch)

    items = build_messenger_targets(42)
    platforms = [item['platform'] for item in items]

    assert platforms == ['telegram', 'max', 'vk']
    assert items[0]['url'].endswith('start=ref_42')
    assert 'start=ref_42' in items[1]['url']
    assert 'start=ref_42' in items[2]['url']


def test_site_entry_targets_include_all_configured_messengers(monkeypatch):
    _configure_all_targets(monkeypatch)

    items = build_site_entry_targets()

    assert [item['platform'] for item in items] == ['telegram', 'max', 'vk']
    assert items[0]['url'] == 'https://t.me/metro_test_bot?start=site'
    assert items[1]['url'] == 'https://max.ru/metrotherapy?start=site'
    assert items[2]['url'] == 'https://vk.com/im?sel=-123456&start=site'


def test_owner_entry_targets_use_explicit_owner_payload(monkeypatch):
    _configure_all_targets(monkeypatch)

    assert build_owner_entry_payload() == 'cpo_site'
    items = build_owner_entry_targets('landing')

    assert [item['platform'] for item in items] == ['telegram', 'max', 'vk']
    assert items[0]['url'] == 'https://t.me/metro_test_bot?start=cpo_landing'
    assert items[1]['url'] == 'https://max.ru/metrotherapy?start=cpo_landing'
    assert items[2]['url'] == 'https://vk.com/im?sel=-123456&ref=cpo_landing'
    assert build_owner_entry_target('vk', 'landing') == items[2]
    assert build_owner_entry_target('unknown', 'landing') is None


def test_share_targets_route_to_selected_platform(monkeypatch):
    _configure_all_targets(monkeypatch)

    items = build_share_targets(42, text='Привет из Метротерапии')
    by_platform = {item['platform']: item for item in items}

    assert by_platform['telegram']['url'].startswith('https://t.me/share/url?')
    assert 'https%3A%2F%2Ft.me%2Fmetro_test_bot%3Fstart%3Dref_42' in by_platform['telegram']['url']
    assert by_platform['vk']['url'].startswith('https://vk.com/share.php?')
    assert 'https%3A%2F%2Fvk.com%2Fim%3Fsel%3D-123456%26start%3Dref_42' in by_platform['vk']['url']
    assert by_platform['max']['url'] == 'https://max.ru/metrotherapy?start=ref_42'
    assert by_platform['max']['entry_url'] == by_platform['max']['url']


def test_gift_targets_route_gift_payload_to_all_configured_messengers(monkeypatch):
    _configure_all_targets(monkeypatch)

    items = build_gift_targets('abc123')

    assert [item['platform'] for item in items] == ['telegram', 'max', 'vk']
    assert items[0]['url'] == 'https://t.me/metro_test_bot?start=gift_abc123'
    assert items[1]['url'] == 'https://max.ru/metrotherapy?start=gift_abc123'
    assert items[2]['url'] == 'https://vk.com/im?sel=-123456&start=gift_abc123'


def test_gift_share_targets_open_selected_messenger_share_or_entry(monkeypatch):
    _configure_all_targets(monkeypatch)

    items = build_gift_share_targets('abc123', text='Подарок Метротерапии')
    by_platform = {item['platform']: item for item in items}

    assert by_platform['telegram']['url'].startswith('https://t.me/share/url?')
    assert 'gift_abc123' in by_platform['telegram']['url']
    assert by_platform['vk']['url'].startswith('https://vk.com/share.php?')
    assert 'gift_abc123' in by_platform['vk']['url']
    assert by_platform['max']['url'] == 'https://max.ru/metrotherapy?start=gift_abc123'
