from __future__ import annotations

import json

from runtime.messenger_vk_sender import _callback_keyboard_json

VK_MAX_BUTTONS_PER_ROW = 5
VK_MAX_BUTTON_ROWS = 6
VK_MOBILE_FALLBACK_BUTTONS_PER_ROW = 2


def _button(index: int) -> dict:
    return {
        "action": {
            "type": "text",
            "label": f"B{index}",
            "payload": json.dumps({"command": f"cmd_{index}"}),
        }
    }


def test_vk_sender_packs_raw_text_keyboards_into_vk_row_limits() -> None:
    raw = {
        "one_time": False,
        "inline": False,
        "buttons": [[_button(index)] for index in range(8)],
    }

    keyboard = json.loads(_callback_keyboard_json(json.dumps(raw)))

    assert keyboard["inline"] is True
    assert len(keyboard["buttons"]) <= VK_MAX_BUTTON_ROWS
    assert all(len(row) <= VK_MOBILE_FALLBACK_BUTTONS_PER_ROW for row in keyboard["buttons"])
    commands = [
        json.loads(button["action"]["payload"])["command"]
        for row in keyboard["buttons"]
        for button in row
    ]
    assert commands == [f"cmd_{index}" for index in range(8)]
    assert {button["action"]["type"] for row in keyboard["buttons"] for button in row} == {"callback"}
