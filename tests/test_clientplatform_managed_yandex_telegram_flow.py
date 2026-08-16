from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.ad_connections import AdPublicationStatus
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


class ManagedYandexTelegramFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_selection_skips_legacy_campaign_listing(self) -> None:
        cb = callback("cpa:conn:0")
        out = SimpleNamespace(answer=AsyncMock())
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "connection_ids": ["connection-1"],
            }
        )

        with (
            patch.object(ad_handlers, "_message", return_value=out),
            patch.object(ad_handlers.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(ad_handlers, "list_yandex_direct_campaigns") as legacy_list,
        ):
            await ad_handlers.choose_managed_yandex_connection(cb, state)

        legacy_list.assert_not_called()
        self.assertEqual(state.data["connection_id"], "connection-1")
        self.assertEqual(state.state, ad_handlers.AdConnectionState.waiting_regions)
        cb.answer.assert_awaited_once_with()
        rendered = out.answer.await_args.args[0]
        self.assertIn("управляемую кампанию", rendered)
        self.assertIn("автоматически не запускаются", rendered)

    async def test_new_region_flow_creates_managed_draft_without_campaign_id(self) -> None:
        msg = message("47, 213")
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "promotion_campaign_id": "promotion-1",
                "connection_id": "connection-1",
                "source_url": "https://t.me/clientplatform_bot?start=safe",
            }
        )
        job = SimpleNamespace(
            id="job-1",
            region_ids=(47, 213),
            title="Свободное время",
            text="Запишитесь онлайн",
            source_url="https://t.me/clientplatform_bot?start=safe",
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

        legacy_create.assert_not_called()
        managed_create.assert_called_once_with(
            actor="actor",
            promotion_campaign_id="promotion-1",
            connection_id="connection-1",
            region_ids=(47, 213),
            source_url="https://t.me/clientplatform_bot?start=safe",
        )
        self.assertEqual(state.state, ad_handlers.AdConnectionState.confirming_publication)
        self.assertEqual(state.data["job_id"], "job-1")
        rendered = msg.answer.await_args.args[0]
        self.assertIn("Проверьте рекламный черновик", rendered)
        self.assertIn("DRAFT", rendered)
        self.assertIn("расходов автоматически не будет", rendered)

    async def test_confirmation_remains_explicit_and_queues_existing_job(self) -> None:
        cb = callback("cpa:confirm")
        out = SimpleNamespace(answer=AsyncMock())
        state = FakeState(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "job_id": "job-1",
                "creative_job_id": "",
            }
        )
        queued = SimpleNamespace(status=AdPublicationStatus.QUEUED)

        with (
            patch.object(ad_handlers.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(
                ad_handlers.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(ad_handlers.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(ad_handlers, "_message", return_value=out),
            patch.object(ad_handlers, "confirm_ad_publication", return_value=queued) as confirm,
        ):
            await ad_handlers.confirm_yandex_publication(cb, state)

        confirm.assert_called_once_with(actor="actor", job_id="job-1")
        self.assertTrue(state.cleared)
        cb.answer.assert_awaited_once_with("Черновик принят")
        self.assertIn("отдельно проверить и запустить", out.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
