from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from services.messenger import reply_dispatcher


class WebinarVenueNativeLinkTests(unittest.TestCase):
    def _interaction(self, command: str) -> CustomerInteractionMessage:
        return CustomerInteractionMessage(
            text="Выберите площадку",
            rows=((CustomerInteractionButton(label="Открыть", command=command),),),
        )

    def test_venue_command_materializes_to_https_link(self) -> None:
        links = reply_dispatcher._clientplatform_runtime_button_links(
            self._interaction("cpm:event-venue-open:telemost"),
            business_id="11111111-1111-4111-8111-111111111111",
        )
        self.assertEqual(
            links["cpm:event-venue-open:telemost"],
            "https://telemost.yandex.ru/",
        )

    def test_vk_and_max_render_venue_as_native_link_button(self) -> None:
        interaction = self._interaction("cpm:event-venue-open:zoom")
        links = {"cpm:event-venue-open:zoom": "https://zoom.us/meeting/schedule"}
        vk = json.loads(
            reply_dispatcher._vk_clientplatform_keyboard(
                interaction,
                button_links=links,
                business_id="11111111-1111-4111-8111-111111111111",
            )
        )
        self.assertEqual(vk["buttons"][0][0]["action"]["type"], "open_link")
        self.assertEqual(
            vk["buttons"][0][0]["action"]["link"],
            "https://zoom.us/meeting/schedule",
        )
        max_payload = reply_dispatcher._max_clientplatform_attachments(
            interaction,
            button_links=links,
            business_id="11111111-1111-4111-8111-111111111111",
        )
        button = max_payload[0]["payload"]["buttons"][0][0]
        self.assertEqual(button["type"], "link")
        self.assertEqual(button["url"], "https://zoom.us/meeting/schedule")

    def test_ucr_fails_closed_until_public_room_link_exists(self) -> None:
        with self.assertRaisesRegex(ValueError, "could not be resolved"):
            reply_dispatcher._clientplatform_runtime_button_links(
                self._interaction("cpm:event-venue-open:ucr"),
                business_id="11111111-1111-4111-8111-111111111111",
            )


if __name__ == "__main__":
    unittest.main()
