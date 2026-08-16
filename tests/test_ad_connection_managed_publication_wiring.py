from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import clientplatform_ad_connections as ad_handlers


class FakeState:
    def __init__(self, data=None) -> None:
        self.data = dict(data or {})
        self.state = None
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def set_data(self, data):
        self.data = dict(data)

    async def update_data(self, **values):
        self.data.update(values)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.cleared = True
        self.data.clear()


async def immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def callback(data: str):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def message(text: str):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def outbound_message():
    return SimpleNamespace(answer=AsyncMock())


class ManagedYandexPublicationWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_selection_offers_managed_and_existing_modes(self) -> None:
        cb = callback("cpa:conn:0")
        out = outbound_message()
        state = FakeState(
            {
                "business_token": "business-1",
                "connection_ids": ["connection-1"],
            }
        )
        with (
            patch.object(ad_handlers, "_message", return_value=out),
            patch.object(ad_handlers.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(ad_handlers, "list_yandex_direct_campaigns") as legacy_list,
        ):
            await ad_handlers.choose_yandex_publication_mode(cb, state)

        legacy_list.assert_not_called()
        self.assertEqual(state.data["connection_id"], "connection-1")
        self.assertEqual(
            state.state,
            ad_handlers.AdConnectionState.selecting_campaign_mode,
        )
        rendered = out.answer.await_args.args[0]
        self.assertIn("управляемая кампания ClientPlatform", rendered)
        rows = out.answer.await_args.kwargs["reply_markup"]
        callbacks = [item[1] for row in rows for item in row]
        self.assertIn(ad_handlers._MANAGED_MODE_CALLBACK, callbacks)
        self.assertIn(ad_handlers._EXISTING_MODE_CALLBACK, callbacks)

    async def test_managed_mode_skips_campaign_listing_and_requests_regions(self) -> None:
        cb = callback(ad_handlers._MANAGED_MODE_CALLBACK)
        out = outbound_message()
        state = FakeState(
            {
                "business_token": "business-1",
                "connection_id": "connection-1",
            }
        )
        with (
            patch.object(ad_handlers, "_message", return_value=out),
            patch.object(ad_handlers, "list_yandex_direct_campaigns") as legacy_list,
        ):
            await ad_handlers.choose_managed_yandex_campaign(cb, state)

        legacy_list.assert_not_called()
        self.assertEqual(state.data["publication_mode"], "managed")
        self.assertEqual(state.state, ad_handlers.AdConnectionState.waiting_regions)
        self.assertIn("Нижний Новгород", out.answer.await_args.args[0])

    async def test_managed_regions_create_managed_draft_not_legacy_draft(self) -> None:
        msg = message("47, 213")
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "promotion_campaign_id": "promotion-1",
                "connection_id": "connection-1",
                "publication_mode": "managed",
                "source_url": "https://t.me/clientplatform_bot?start=source",
            }
        )
        job = SimpleNamespace(
            id="job-1",
            region_ids=(47, 213),
            title="Заголовок",
            text="Текст",
            source_url="https://t.me/clientplatform_bot?start=source",
        )
        draft = SimpleNamespace(job=job, campaign_name="ClientPlatform · promotion-1")
        with (
            patch.object(ad_handlers.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(
                ad_handlers.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(ad_handlers.control, "_user_id", return_value=101),
            patch.object(ad_handlers.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(
                ad_handlers,
                "create_managed_ad_publication_draft",
                return_value=draft,
            ) as managed_create,
            patch.object(ad_handlers, "create_ad_publication_draft") as legacy_create,
        ):
            await ad_handlers.prepare_ad_publication(msg, state)

        managed_create.assert_called_once_with(
            actor="actor",
            promotion_campaign_id="promotion-1",
            connection_id="connection-1",
            region_ids=(47, 213),
            source_url="https://t.me/clientplatform_bot?start=source",
        )
        legacy_create.assert_not_called()
        self.assertEqual(
            state.state,
            ad_handlers.AdConnectionState.confirming_publication,
        )
        rendered = msg.answer.await_args.args[0]
        self.assertIn("отключённую управляемую кампанию", rendered)
        self.assertIn("расходов автоматически не будет", rendered)

    async def test_existing_mode_remains_available(self) -> None:
        cb = callback(ad_handlers._EXISTING_MODE_CALLBACK)
        out = outbound_message()
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "connection_id": "connection-1",
            }
        )
        campaign = SimpleNamespace(
            campaign_id="6001",
            name="Существующая",
            state="ON",
        )
        with (
            patch.object(ad_handlers.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(
                ad_handlers.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(ad_handlers.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(ad_handlers, "_message", return_value=out),
            patch.object(
                ad_handlers,
                "list_yandex_direct_campaigns",
                return_value=[campaign],
            ) as legacy_list,
        ):
            await ad_handlers.choose_yandex_campaign(cb, state)

        legacy_list.assert_called_once_with(
            actor="actor",
            connection_id="connection-1",
        )
        self.assertEqual(state.data["publication_mode"], "existing")
        self.assertEqual(state.state, ad_handlers.AdConnectionState.selecting_campaign)
        self.assertEqual(state.data["yandex_campaigns"][0]["id"], "6001")

    async def test_missing_publication_mode_fails_closed(self) -> None:
        msg = message("47")
        state = FakeState(
            {
                "business_id": "business-1",
                "connection_id": "connection-1",
            }
        )
        with (
            patch.object(ad_handlers, "create_managed_ad_publication_draft") as managed_create,
            patch.object(ad_handlers, "create_ad_publication_draft") as legacy_create,
        ):
            await ad_handlers.prepare_ad_publication(msg, state)

        managed_create.assert_not_called()
        legacy_create.assert_not_called()
        self.assertTrue(state.cleared)
        self.assertIn("никаких действий", msg.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
