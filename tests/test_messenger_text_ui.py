from services.messenger.text_ui import handle_incoming_text
from services.messenger.preferences import get_channel_snapshot
from services.messenger.menu_contract import MAIN_MENU_ACTIONS
from services.schema import init_db


def setup_module(module):
    init_db()


def test_platform_command_updates_preference():
    canonical_user_id, replies = handle_incoming_text(901001, platform='vk', external_user_id='901001', text='/platform max')
    assert canonical_user_id != 901001
    assert replies
    snapshot = get_channel_snapshot(canonical_user_id)
    assert snapshot['preferred_platform'] == 'max'


def test_share_command_returns_targets_text():
    canonical_user_id, replies = handle_incoming_text(901002, platform='max', external_user_id='901002', text='share')
    assert canonical_user_id != 901002
    assert replies
    assert 'Поделиться' in replies[0].text or 'пока не настроены' in replies[0].text


def test_continue_command_returns_next_audio_action():
    canonical_user_id, replies = handle_incoming_text(901003, platform='telegram', external_user_id='901003', text='continue')
    assert canonical_user_id == 901003
    assert replies[0].kind == 'next_audio'


def test_vk_and_max_accept_all_canonical_main_menu_titles(monkeypatch):
    monkeypatch.setenv("PAYMENT_HTTP_ENABLED", "1")
    expected_fragments = {
        'demo': 'Бесплатная практика',
        'full': 'Полный маршрут',
        'pay': 'Тарифы Метротерапии',
        'gift': 'Подарить Метротерапию',
        'progress': 'прогресс',
        'settings': 'Настройки канала',
        'share': 'Поделиться',
        'weather': 'Погода',
    }

    for platform in ('vk', 'max'):
        for offset, action in enumerate(MAIN_MENU_ACTIONS, start=1):
            canonical_user_id = 902000 + offset + (100 if platform == 'max' else 0)
            _, replies = handle_incoming_text(
                canonical_user_id,
                platform=platform,
                external_user_id=str(canonical_user_id),
                text=action.title,
            )
            assert replies, (platform, action.command)
            assert expected_fragments[action.command].casefold() in replies[0].text.casefold(), (platform, action.command, replies[0].text)


def test_vk_and_max_accept_context_buttons_by_title():
    for platform in ('vk', 'max'):
        _, replies = handle_incoming_text(903001, platform=platform, external_user_id='903001', text='🎧 Получить аудио')
        assert replies[0].kind == 'text'
        assert 'Бесплатная практика' in replies[0].text or replies[0].kind == 'next_audio'

        _, replies = handle_incoming_text(903002, platform=platform, external_user_id='903002', text='✅ Прослушал')
        assert replies
        assert replies[0].kind in {'text', 'auto_post_score'}
