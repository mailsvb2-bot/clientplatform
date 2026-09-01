from __future__ import annotations

import json

from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from runtime.messenger_vk_sender import _callback_keyboard_json
from services.messenger.reply_dispatcher import _vk_clientplatform_keyboard


def test_vk_inline_keyboard_drops_one_time_before_provider_send() -> None:
    raw = json.dumps(
        {
            'one_time': True,
            'inline': True,
            'buttons': [[{'action': {'type': 'text', 'label': 'Open', 'payload': '{}'}}]],
        }
    )
    payload = json.loads(_callback_keyboard_json(raw))
    assert payload['inline'] is True
    assert 'one_time' not in payload
    assert payload['buttons'][0][0]['action']['type'] == 'callback'


def test_canonical_clientplatform_keyboard_is_provider_valid() -> None:
    interaction = CustomerInteractionMessage(
        text='ClientPlatform',
        rows=((CustomerInteractionButton(label='Открыть', command='business'),),),
    )
    raw = _vk_clientplatform_keyboard(
        interaction,
        button_links={},
        business_id='',
    )
    wire = json.loads(_callback_keyboard_json(raw))
    assert wire['inline'] is True
    assert 'one_time' not in wire
    assert wire['buttons']
    assert wire['buttons'][0][0]['action']['type'] == 'callback'
