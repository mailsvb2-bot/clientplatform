from pathlib import Path

from services.schema import init_db
from services.mood import create_session, set_pre, mark_audio_sent
from services.messenger.audio_progress import AudioProgressItem, mark_pending_audio_delivery
from services.messenger.entrypoints import register_user_entry
from services.messenger.text_ui import handle_incoming_text


def setup_module(module):
    init_db()


def test_done_with_pending_post_requests_post_score_in_text_channels():
    raw_user_id = 930001
    account_id = register_user_entry(
        raw_user_id, platform='max', external_user_id=str(raw_user_id)
    ).user_id
    sid = create_session(account_id, kind='work', source='auto', day='2026-04-16', slot='morning', anchor_id=11)
    assert set_pre(sid, 3)
    mark_audio_sent(sid)
    item = AudioProgressItem(ordinal=1, anchor=11, title='A11', path=Path('audio/full/clientplatform1.opus'))
    mark_pending_audio_delivery(account_id, item=item, platform='max', token=None)

    canonical_user_id, replies = handle_incoming_text(raw_user_id, platform='max', external_user_id=str(raw_user_id), text='done')

    assert canonical_user_id == account_id
    assert len(replies) == 1
    assert 'оцените состояние после прослушивания' in replies[0].text.lower()


def test_numeric_reply_routes_to_post_score_when_post_is_pending():
    raw_user_id = 930002
    account_id = register_user_entry(
        raw_user_id, platform='vk', external_user_id=str(raw_user_id)
    ).user_id
    sid = create_session(account_id, kind='home', source='auto', day='2026-04-16', slot='evening', anchor_id=12)
    assert set_pre(sid, -1)
    mark_audio_sent(sid)

    canonical_user_id, replies = handle_incoming_text(raw_user_id, platform='vk', external_user_id=str(raw_user_id), text='4')

    assert canonical_user_id == account_id
    assert replies[0].kind == 'auto_post_score'
    assert replies[0].meta['score'] == '4'
