from pathlib import Path

from services.schema import init_db
from services.messenger.audio_access import get_audio_access_grant, issue_or_reuse_audio_access_token, register_audio_access
from services.messenger.audio_progress import AudioProgressItem, confirm_pending_audio_delivery, get_progress_snapshot


def setup_module(module):
    init_db()


def test_reuses_pending_token_before_and_after_url_access():
    item = AudioProgressItem(ordinal=1, anchor=10, title="A10", path=Path("audio/full/clientplatform0.opus"))
    token1 = issue_or_reuse_audio_access_token(8111, item=item, platform="vk")
    token2 = issue_or_reuse_audio_access_token(8111, item=item, platform="vk")
    assert token1 == token2

    accessed = register_audio_access(token1)
    assert accessed is not None
    token3 = issue_or_reuse_audio_access_token(8111, item=item, platform="vk")
    assert token3 == token1

    snap = get_progress_snapshot(8111)
    assert snap.last_anchor is None
    assert snap.pending_item is not None
    assert snap.pending_item.anchor == 10


def test_url_access_never_confirms_progress():
    item = AudioProgressItem(ordinal=1, anchor=11, title="A11", path=Path("audio/full/clientplatform1.opus"))
    token = issue_or_reuse_audio_access_token(8112, item=item, platform="max")
    grant1 = register_audio_access(token)
    grant2 = register_audio_access(token)
    assert grant1 is not None and grant2 is not None

    refreshed = get_audio_access_grant(token)
    assert refreshed is not None
    assert refreshed.access_count == 2

    snap = get_progress_snapshot(8112)
    assert snap.last_anchor is None
    assert snap.pending_item is not None
    assert snap.pending_item.anchor == 11

    confirmed = confirm_pending_audio_delivery(8112, platform="max")
    assert confirmed is not None
    assert confirmed.anchor == 11
    completed = get_progress_snapshot(8112)
    assert completed.last_anchor == 11
    assert completed.pending_item is None
