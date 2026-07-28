from pathlib import Path

from services.schema import init_db
from services.accounts.identity import resolve_account_for_identity
from services.messenger.audio_progress import AudioProgressItem, get_progress_snapshot, mark_pending_audio_delivery
from services.messenger.text_ui import handle_incoming_text


def setup_module(module):
    init_db()


def test_done_command_confirms_pending_and_requests_next_audio():
    item = AudioProgressItem(ordinal=1, anchor=11, title="A11", path=Path("audio/full/clientplatform1.opus"))
    mark_pending_audio_delivery(910001, item=item, platform='telegram', token=None)

    canonical_user_id, replies = handle_incoming_text(910001, platform='telegram', external_user_id='910001', text='done')

    assert canonical_user_id == 910001
    assert replies[0].kind == 'text'
    assert 'Подтвердил аудио' in replies[0].text
    assert replies[1].kind == 'next_audio'
    snap = get_progress_snapshot(910001)
    assert snap.last_anchor == 11
    assert snap.pending_item is None


def test_done_command_without_pending_returns_hint():
    canonical_user_id, replies = handle_incoming_text(910002, platform='vk', external_user_id='910002', text='готово')
    assert canonical_user_id >= (1 << 62)
    assert replies and 'нет аудио' in replies[0].text.lower()


def test_vk_done_command_returns_post_score_scale_keyboard():
    item = AudioProgressItem(ordinal=1, anchor=12, title="A12", path=Path("audio/full/clientplatform2.opus"))
    handle_incoming_text(910003, platform='vk', external_user_id='910003', text='menu')
    account_id = resolve_account_for_identity('vk', '910003', allow_create=False)
    assert account_id is not None
    mark_pending_audio_delivery(account_id, item=item, platform='vk', token=None)

    canonical_user_id, replies = handle_incoming_text(910003, platform='vk', external_user_id='910003', text='прослушал')

    assert canonical_user_id == account_id
    assert replies
    assert replies[0].kind == 'text'
    assert 'ПОСЛЕ' in replies[0].text or 'после' in replies[0].text.lower()
    assert replies[0].meta.get('vk_keyboard') == 'score_scale'
